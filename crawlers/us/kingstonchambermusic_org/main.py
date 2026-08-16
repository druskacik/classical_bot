import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kingstonchambermusic.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'Kingston Chamber Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', clean_text(value), re.I)
    if not match:
        return None
    normalized = re.sub(r'\s+', ' ', match.group(1)).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_detail(record, session):
    url = record.get('link', '')
    if not url.startswith(('http://', 'https://')):
        return None

    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title = clean_text(record.get('title', {}).get('rendered'))
    details = {}
    for item in soup.select('.event-list-details li'):
        label = item.find('small')
        if label:
            details[clean_text(label).rstrip(':').lower()] = clean_text(item).removeprefix(
                clean_text(label)
            ).strip()

    event_date = parse_date(details.get('date'))
    time_from = parse_time(details.get('time'))

    article = soup.select_one('article#article')
    location_heading = None
    if article:
        location_heading = next(
            (
                node for node in article.select('h4, h5, h6')
                if clean_text(node).lower().startswith('location:')
            ),
            None,
        )
    content_html = record.get('content', {}).get('rendered', '')
    content_soup = BeautifulSoup(content_html, 'html.parser')
    content_location = next(
        (
            clean_text(node) for node in content_soup.select('p, h4, h5, h6')
            if clean_text(node).lower().startswith('location:')
        ),
        '',
    )
    location_text = clean_text(location_heading) or content_location
    location_text = re.sub(r'^location:\s*', '', location_text, flags=re.I)
    venue = clean_text(location_text.split(',', 1)[0])

    city_node = soup.select_one('.segment-city')
    city = clean_text(city_node)
    if not city and re.search(r'\bKingston\b', location_text, re.I):
        city = 'Kingston'

    description = clean_text(content_html) or None
    if re.search(r'\bLutheran Church of the Good Shepherd\b', description or '', re.I):
        venue = 'Lutheran Church of the Good Shepherd'
        city = 'Kingston'
    elif re.search(r'\bGood Shepherd Lutheran Church\b', location_text, re.I):
        venue = 'Good Shepherd Lutheran Church'
        city = 'Kingston'

    # Older detail templates expose only the geocoded street address below the
    # programme. These two addresses are explicitly identified elsewhere on
    # the same event page/site as their named venues.
    if not venue and city == 'South Kingstown':
        venue = 'URI Fine Arts Center Concert Hall'
    elif not venue and city == 'Jamestown':
        venue = 'Jamestown Arts Center'

    if not all((title, event_date, venue, city)):
        log_message(
            'Skipping event with incomplete required details',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    api_records = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        api_records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages or not batch:
            break
        page += 1

    records = []
    for api_record in api_records:
        try:
            parsed = event_detail(api_record, session)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=api_record.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if parsed:
            records.append(parsed)

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class KingstonChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kingstonchambermusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    KingstonChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
