import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kznphil.org.za/'
SOURCE = 'KwaZulu-Natal Philharmonic Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'

# The site uses category 10 for current in-person concerts and moves old event
# posts to category 12.  Category 12 also contains a few non-event articles,
# which is why records go through the potential-event classifier.
EVENT_CATEGORIES = '10,12'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup(['style', 'script']):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'categories': EVENT_CATEGORIES,
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,content,categories',
            },
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages or not batch:
            break
        page += 1
    return posts


def parse_dates(text):
    # Event copy consistently puts its occurrence date near the beginning.
    head = text[:700]
    pattern = re.compile(
        r'\b(\d{1,2})(?:st|nd|rd|th)?'
        r'(?:\s*(?:-|–|—|and|&)\s*(\d{1,2})(?:st|nd|rd|th)?)?'
        r'\s+(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(20\d{2})\b',
        re.IGNORECASE,
    )
    match = pattern.search(head)
    if not match:
        return []

    first_day = int(match.group(1))
    last_day = int(match.group(2) or first_day)
    month = MONTHS[match.group(3).lower()]
    year = int(match.group(4))
    if last_day < first_day or last_day - first_day > 14:
        last_day = first_day

    dates = []
    for day in range(first_day, last_day + 1):
        try:
            dates.append(date(year, month, day).isoformat())
        except ValueError:
            return []
    return dates


def parse_time(text):
    head = text[:800]
    match = re.search(r'\b([01]?\d|2[0-3])\s*[h:.]\s*([0-5]\d)\b', head, re.IGNORECASE)
    if match:
        return f'{int(match.group(1)):02d}:{match.group(2)}'

    match = re.search(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)\b', head, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def resolve_location(title, text):
    evidence = f'{title}\n{text[:1800]}'
    locations = (
        (r'Chelsea Preparatory School', 'Chelsea Preparatory School', 'Durban North'),
        (r'Azalea Hall[^\n,]*(?:,\s*)?Margate Retirement Village', 'Azalea Hall, Margate Retirement Village', 'Margate'),
        (r'Camp Orchards', 'Camp Orchards', 'Hillcrest'),
        (r'(?:The\s+)?Junction\s*\(\s*St\s+Agnes\s+Church\s*\)', 'The Junction (St Agnes Church)', 'Kloof'),
        (r'Durban ICC', 'Durban ICC', 'Durban'),
        (r'Playhouse\s+Opera\s+Theatre', 'Playhouse Opera Theatre', 'Durban'),
        (r'Playhouse\s+Drama\s+Theatre', 'Playhouse Drama Theatre', 'Durban'),
        (r'St\s+Thomas(?:\x27s|’s)?\s+Church(?:\s+Musgrave)?', "St Thomas's Church", 'Durban'),
    )
    for pattern, venue, city in locations:
        if re.search(pattern, evidence, re.IGNORECASE):
            return venue, city

    # World Symphony Series concerts are the orchestra's home subscription
    # series at the Playhouse. Touring and suburban titles never use this
    # default, and their explicit locations are handled above.
    if re.search(r'\b(?:WSS|World Symphony Series)\b', title, re.IGNORECASE):
        return 'Playhouse Opera Theatre', 'Durban'
    return None, None


def make_records(post):
    title = clean_text((post.get('title') or {}).get('rendered'))
    description = clean_text((post.get('content') or {}).get('rendered'))
    url = post.get('link') or ''
    if not title or not description or not url:
        return []

    # The archive includes streamed recordings and several general news posts.
    # Require a physical performance location so neither kind becomes a row.
    venue, city = resolve_location(title, description)
    dates = parse_dates(description)
    if not venue or not city or not dates:
        return []

    time_from = parse_time(description)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'ZA',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = get_posts(session)
    records = []
    for post in posts:
        try:
            records.extend(make_records(post))
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse concert post',
                event='crawler_item_failed',
                level='warning',
                url=post.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class KznphilOrgZaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kznphil_org_za',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ZA',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KznphilOrgZaCrawler().run()


if __name__ == '__main__':
    main()
