import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cantonsymphony.org/'
SOURCE = 'Canton Symphony Orchestra'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
SUMMER_URL = f'{SOURCE_URL}summer-serenades/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.I)
OH_ADDRESS_RE = re.compile(r'\b([^\n|]{2,80}?)\s+\d{5}(?:-\d{4})?\b')


def clean_text(value, separator='\n'):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json(), response.headers


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def api_events(session):
    # The old start date asks TEC for every occurrence it still retains. The
    # API may paginate if the calendar grows beyond its present size.
    page = 1
    events = []
    while True:
        payload, _ = get_json(
            session,
            EVENTS_API,
            {'start_date': '2000-01-01', 'per_page': 50, 'page': page},
        )
        events.extend(payload.get('events') or [])
        if page >= int(payload.get('total_pages') or 0):
            break
        page += 1
    return events


def published_pages(session):
    pages = []
    page = 1
    while True:
        payload, headers = get_json(
            session,
            PAGES_API,
            {'per_page': 100, 'page': page, '_fields': 'link,title'},
        )
        pages.extend(payload)
        if page >= int(headers.get('X-WP-TotalPages', 1)):
            break
        page += 1
    return pages


def normalized(value):
    value = clean_text(value, ' ').lower().replace('&', ' and ')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def page_for_event(event, pages):
    if event.get('website'):
        return event['website']

    title = normalized(event.get('title'))
    title_words = set(title.split()) - {'the', 'at', 'a', 'an', 'of'}
    candidates = []
    for page in pages:
        page_title = normalized((page.get('title') or {}).get('rendered'))
        page_words = set(page_title.split()) - {'the', 'at', 'a', 'an', 'of'}
        if not page_words:
            continue
        overlap = len(title_words & page_words) / max(len(title_words), len(page_words))
        if page_title == title:
            overlap += 2
        candidates.append((overlap, page.get('link')))
    score, url = max(candidates, default=(0, None))
    return url if score >= 0.65 else event.get('url')


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    if match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_date(value):
    match = DATE_RE.search(value or '')
    if not match:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def venue_and_city(page_text, fallback_title=''):
    # Most season pages print the hall followed by its Canton postal address.
    if re.search(r'Umstattd Hall|Zimmermann Symphony Center', page_text, re.I):
        return 'Umstattd Hall | Zimmermann Symphony Center', 'Canton'
    if re.search(r'\bOnesto\b', page_text + ' ' + fallback_title, re.I):
        return 'The Onesto', 'Canton'
    return None, None


def page_description(soup, fallback=None):
    main = soup.select_one('main') or soup.select_one('#content')
    if main:
        for unwanted in main.select('nav, script, style, form'):
            unwanted.decompose()
        text = clean_text(main.get_text('\n', strip=True))
        if text:
            return text
    return clean_text(fallback) or None


def make_api_records(session, events, pages):
    records = []
    for event in events:
        title = clean_text(event.get('title'), ' ')
        # The API's summer entries omit venue data and redirect to a shared
        # page. That page is parsed below, including occurrences older than
        # the API's retained range.
        if title.lower().startswith('summer serenades:'):
            continue
        start = event.get('start_date') or ''
        try:
            start_at = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            continue

        url = page_for_event(event, pages)
        description = clean_text(event.get('description')) or None
        page_text = ''
        if url:
            try:
                soup = get_soup(session, url)
                page_text = clean_text(soup.get_text('\n', strip=True))
                description = page_description(soup, description)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Canton Symphony event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        venue, city = venue_and_city(page_text, title)
        if not venue or not city:
            # Event pages without a separate landing page use the orchestra's
            # home hall. Summer tour stops are supplied by the overview parser.
            venue = 'Umstattd Hall | Zimmermann Symphony Center'
            city = 'Canton'

        if not title or not url:
            continue
        records.append({
            'title': title,
            'date': start_at.date().isoformat(),
            'url': url,
            'time_from': start_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


def summer_records(session):
    soup = get_soup(session, SUMMER_URL)
    records = []
    for heading in soup.select('h2, h3, h4'):
        venue = clean_text(heading.get_text(' ', strip=True), ' ').lstrip('*').strip()
        if not venue:
            continue

        container = heading.parent
        for _ in range(4):
            text = clean_text(container.get_text(' ', strip=True), ' ')
            if DATE_RE.search(text) and re.search(r'\bOH\s+\d{5}\b', text):
                break
            if not container.parent:
                break
            container = container.parent
        else:
            continue

        event_date = parse_date(text)
        time_from = parse_time(text)
        city_match = re.search(
            r'\b([A-Za-z][A-Za-z .\'-]{1,30}),\s*OH\s+\d{5}\b', text, re.I
        )
        city = clean_text(city_match.group(1), ' ') if city_match else ''
        if re.search(r'\b(?:N|S|E|W|NE|NW|SE|SW)\s+Canton$', city, re.I):
            city = 'Canton'
        if not event_date or not time_from or not city:
            continue
        records.append({
            'title': f'Summer Serenades: {venue}',
            'date': event_date,
            'url': SUMMER_URL,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': text,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = api_events(session)
    pages = published_pages(session)
    records = make_api_records(session, events, pages)
    records.extend(summer_records(session))
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class CantonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cantonsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CantonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
