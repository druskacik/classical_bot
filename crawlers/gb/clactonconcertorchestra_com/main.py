import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://clactonconcertorchestra.com/'
SOURCE = 'Clacton Concert Orchestra'
API_URL = (
    'https://public-api.wordpress.com/rest/v1.1/sites/'
    'clactonconcertorchestra.com/posts/'
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b(?:(?:Mon|Tues?|Wednes|Thurs?|Fri|Satur|Sun)day,?\s+)?'
    rf'(?:({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?|'
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTHS}))'
    rf'(?:\s*,?\s*(20\d{{2}}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(?:at\s+)?(\d{1,2})(?:[.:](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b',
    re.IGNORECASE,
)

# The archive uses a small, recurring set of venues. Mapping the first-party
# venue names avoids mistaking street/address text for a city.
VENUES = (
    (re.compile(r"St\.?\s+Paul(?:'s|’s|s)?\s+Church", re.I), "St Paul's Church", 'Clacton-on-Sea'),
    (re.compile(r"St\.?\s+Bartholomew(?:'s|’s|s)?\s+Church", re.I), "St Bartholomew's Church", 'Holland-on-Sea'),
    (re.compile(r"St\.?\s+James(?:'s|’s|s)?\s+Church", re.I), "St James' Church", None),
    (re.compile(r"St\.?\s+Mary(?:'s|’s|s)?\s+Church", re.I), "St Mary's Church", 'Frinton-on-Sea'),
    (re.compile(r"St\.?\s+Nicholas\s+Church", re.I), 'St Nicholas Church', 'Harwich'),
    (re.compile(r"West\s+Cliff\s+Theatre", re.I), 'West Cliff Theatre', 'Clacton-on-Sea'),
    (re.compile(r"Princes\s+Theatre", re.I), 'Princes Theatre', 'Clacton-on-Sea'),
    (re.compile(r"Sunspot\s+cent(?:re|er)", re.I), 'Sunspot Centre', 'Jaywick Sands'),
    (re.compile(r"Public\s+Hall", re.I), 'Public Hall', 'Clacton-on-Sea'),
    (re.compile(r"parish\s+church\s+of\s+St\.?\s+Osyth", re.I), 'Parish Church of St Osyth', 'St Osyth'),
)
CITY_RE = re.compile(
    r'\b(Clacton(?:-on-Sea| on Sea)?|Holland-on-Sea|Brightlingsea|'
    r'Frinton-on-Sea|Harwich|Jaywick Sands|St Osyth)\b',
    re.IGNORECASE,
)


def clean_text(value):
    soup = BeautifulSoup(value or '', 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    text = html.unescape(soup.get_text('\n', strip=True))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def infer_year(post):
    title_match = re.search(r'\b(20\d{2})\b', clean_text(post.get('title')))
    if title_match:
        return int(title_match.group(1))
    return datetime.fromisoformat(post['date']).year


def parse_date(match, default_year):
    month = match.group(1) or match.group(4)
    day = match.group(2) or match.group(3)
    year = int(match.group(5) or default_year)
    try:
        return datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour not in range(1, 13) or minute > 59:
        return None
    if match.group(3).lower().startswith('p') and hour != 12:
        hour += 12
    elif match.group(3).lower().startswith('a') and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def normalise_city(city):
    key = re.sub(r'[ -]', '', city).lower()
    return {
        'clacton': 'Clacton-on-Sea',
        'clactononsea': 'Clacton-on-Sea',
        'hollandonsea': 'Holland-on-Sea',
        'brightlingsea': 'Brightlingsea',
        'frintononsea': 'Frinton-on-Sea',
        'harwich': 'Harwich',
        'jaywicksands': 'Jaywick Sands',
        'stosyth': 'St Osyth',
    }.get(key, city)


def location_from_context(context):
    candidates = []
    for pattern, venue, default_city in VENUES:
        match = pattern.search(context)
        if not match:
            continue
        candidates.append((match.start(), match, venue, default_city))
    if candidates:
        _, match, venue, default_city = min(candidates, key=lambda item: item[0])
        nearby = context[match.start():match.end() + 100]
        city_match = CITY_RE.search(nearby)
        city = normalise_city(city_match.group(1)) if city_match else default_city
        if venue == "St James' Church" and not city:
            # Both are explicitly used by the archive; only accept a nearby city.
            return None, None
        return venue, city
    return None, None


def parse_post(post):
    content = post.get('content') or ''
    soup = BeautifulSoup(content, 'html.parser')
    blocks = [
        clean_text(str(node))
        for node in soup.select('h1, h2, h3, h4, p, li, figcaption')
        if clean_text(str(node))
    ]
    description = clean_text(content) or None
    title = clean_text(post.get('title'))
    url = (post.get('URL') or '').replace('http://', 'https://', 1)
    default_year = infer_year(post)
    records = []

    for index, block in enumerate(blocks):
        matches = list(DATE_RE.finditer(block))
        for match_index, match in enumerate(matches):
            event_date = parse_date(match, default_year)
            if not event_date:
                continue
            next_start = (
                matches[match_index + 1].start()
                if match_index + 1 < len(matches)
                else min(len(block), match.end() + 500)
            )
            local_block = block[max(0, match.start() - 100):next_start]
            context = '\n'.join(
                [local_block] + blocks[index + 1:index + 3]
            )
            lowered = context.lower()
            if any(word in lowered for word in ('cancelled', 'canceled', 'postponed')):
                continue
            venue, city = location_from_context(context)
            if not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(context),
                'venue': venue,
                'city': city,
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    posts = []
    page = 1
    while True:
        response = requests.get(
            API_URL,
            params={'number': 100, 'page': page},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        page_posts = payload.get('posts', [])
        posts.extend(page_posts)
        if not page_posts or len(posts) >= payload.get('found', len(posts)):
            break
        page += 1
    records = []
    for post in posts:
        try:
            records.extend(parse_post(post))
        except (KeyError, TypeError, ValueError) as error:
            log_message(
                'Failed to parse Clacton Concert Orchestra post',
                event='crawler_item_failed',
                level='warning',
                url=post.get('URL'),
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['venue']),
    )


class ClactonConcertOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='clactonconcertorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ClactonConcertOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
