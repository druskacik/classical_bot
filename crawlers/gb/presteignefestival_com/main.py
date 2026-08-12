import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://presteignefestival.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Presteigne Festival'
EVENTS_CATEGORY_ID = 70

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(\d{1,2})\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'(?:\s+(20\d{2}))?\s*,\s*'
    r'(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b',
    re.IGNORECASE,
)

KNOWN_CITIES = (
    'Presteigne',
    'Bleddfa',
    'Leominster',
    'Leintwardine',
    'Knighton',
    'Norton',
    'Discoed',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, endpoint, params=None):
    response = session.get(f'{API_URL}/{endpoint}', params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def event_posts(session):
    categories = get_json(session, 'categories', {'per_page': 100})
    years = {}
    for category in categories:
        match = re.search(r'\b(20\d{2})\b', clean_text(category.get('name')))
        if match:
            years[category['id']] = int(match.group(1))

    posts = get_json(
        session,
        'posts',
        {'categories': EVENTS_CATEGORY_ID, 'per_page': 100, 'page': 1},
    )
    return [
        (post, next((years[value] for value in post.get('categories', []) if value in years), None))
        for post in posts
        if clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser')).lower() != 'test'
    ]


def parse_datetime(text, year):
    match = DATE_RE.search(text)
    if not match:
        return None
    day, month, explicit_year, hour, minute, meridiem = match.groups()
    event_year = int(explicit_year) if explicit_year else year
    if not event_year:
        return None
    hour = int(hour)
    minute = int(minute or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    try:
        event_date = datetime.strptime(
            f'{day} {month} {event_year}', '%d %B %Y'
        ).date().isoformat()
    except ValueError:
        return None
    return event_date, f'{hour:02d}:{minute:02d}'


def venue_and_city(article):
    venue_node = article.select_one('.venue_select.lang-eng a')
    venue_text = clean_text(venue_node)

    if not venue_text:
        date_node = article.select_one('.date_time')
        following = clean_text(date_node.parent if date_node else '')
        match = re.search(
            r'\b(?:meet|gather)\s+at\s+([^\n]+?)(?=\n|What[’\']s|Tickets?:|$)',
            following,
            re.IGNORECASE,
        )
        venue_text = match.group(1).strip() if match else ''

    city = next(
        (candidate for candidate in KNOWN_CITIES if re.search(rf'\b{re.escape(candidate)}\b', venue_text, re.I)),
        None,
    )
    if not venue_text or not city:
        return None, None

    venue = re.sub(r'^\s*(?:meet|gather)\s+at\s+', '', venue_text, flags=re.IGNORECASE)
    venue = re.split(rf',\s*(?:[^,]+,\s*)?{re.escape(city)}\b', venue, maxsplit=1, flags=re.I)[0]
    venue = re.sub(r'\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\s*$', '', venue, flags=re.I)
    return venue.strip(' ,'), city


def description_text(article):
    text = clean_text(article)
    text = re.split(r'\n(?:Tickets?|Tocynnau):', text, maxsplit=1, flags=re.IGNORECASE)[0]
    return text or None


def parse_event(content, post, category_year):
    soup = BeautifulSoup(content, 'html.parser')
    article = soup.select_one('article.hentry, article')
    if not article:
        return None

    title = clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser'))
    page_text = clean_text(article)
    parsed_datetime = parse_datetime(page_text, category_year)
    venue, city = venue_and_city(article)
    if not title or not parsed_datetime or not venue or not city:
        return None

    event_date, time_from = parsed_datetime
    return {
        'title': title,
        'date': event_date,
        'url': post['link'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description_text(article),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class PresteigneFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='presteignefestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        posts = event_posts(session)
        records = []

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(session.get, post['link'], timeout=45): (post, year)
                for post, year in posts
            }
            for future in as_completed(futures):
                post, year = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_event(response.content, post, year)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Presteigne Festival event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=post['link'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    PresteigneFestivalComCrawler().run()


if __name__ == '__main__':
    main()
