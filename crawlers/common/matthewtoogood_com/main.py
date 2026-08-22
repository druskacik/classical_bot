import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.matthewtoogood.com/'
SOURCE = 'Matthew Toogood'
CALENDAR_API = f'{SOURCE_URL}api/open/GetItemsByMonth'
COLLECTION_ID = '621484ad4b5d2811ea9e059d'
FIRST_ARCHIVE_YEAR = 2022

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRIES = {
    'australia': 'AU',
    'austria': 'AT',
    'germany': 'DE',
}
SITE_TIMEZONE = ZoneInfo('Europe/Berlin')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_country(event):
    location = event.get('location') or {}
    country = clean_text(location.get('addressCountry')).lower()
    if country in COUNTRIES:
        return COUNTRIES[country]

    location_text = ' '.join(
        clean_text(value) for value in (
            location.get('addressTitle'),
            location.get('addressLine1'),
            location.get('addressLine2'),
            event.get('title'),
        )
    ).lower()
    if 'tiroler landestheater' in location_text or 'innsbruck' in location_text:
        return COUNTRIES['austria']
    return None


def resolve_city(event, country_code):
    location = event.get('location') or {}
    line = clean_text(location.get('addressLine2'))
    title = clean_text(event.get('title'))

    if country_code == 'AT' and (
        'tiroler landestheater' in clean_text(location.get('addressLine1')).lower()
        or 'innsbruck' in title.lower()
    ):
        return 'Innsbruck'

    if not line:
        return None
    if country_code == 'DE':
        match = re.search(r'(?:^|,\s*)\d{5}\s+(.+)$', line)
        if match:
            return match.group(1).strip()
        return line.split(',', 1)[0].strip() or None
    if country_code == 'AU':
        # Australian calendar addresses use "Suburb, VIC, postcode" or
        # "Suburb Vic postcode". Preserve the locality supplied by the site.
        city = line.split(',', 1)[0]
        city = re.sub(r'\s+(?:VIC|NSW|QLD|SA|WA|TAS|ACT|NT)\s+\d{4}$', '', city, flags=re.I)
        return city.strip() or None
    return line.split(',', 1)[0].strip() or None


def description_from(event):
    parts = []
    for field in ('detail_description', 'excerpt', 'body'):
        value = clean_text(event.get(field))
        if value and value not in parts:
            parts.append(value)

    categories = [clean_text(value) for value in event.get('categories') or []]
    categories = [value for value in categories if value]
    tags = [clean_text(value) for value in event.get('tags') or []]
    tags = [value for value in tags if value]
    if categories:
        parts.append('Categories: ' + ', '.join(categories))
    if tags:
        parts.append('Tags: ' + ', '.join(tags))
    return '\n\n'.join(parts) or None


def make_record(event):
    title = clean_text(event.get('title'))
    event_path = clean_text(event.get('fullUrl'))
    location = event.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    country = resolve_country(event)
    timestamp = event.get('startDate')
    if not title or not event_path or not venue or not country or not isinstance(timestamp, int):
        return None

    country_code = country
    try:
        # Squarespace renders every calendar timestamp in the site's configured
        # Europe/Berlin timezone, including performances abroad.
        starts_at = datetime.fromtimestamp(timestamp / 1000, SITE_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None
    city = resolve_city(event, country_code)
    if not city:
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': requests.compat.urljoin(SOURCE_URL, event_path),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_month(month):
    response = requests.get(
        CALENDAR_API,
        params={'month': month, 'collectionId': COLLECTION_ID},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f'Unexpected calendar response for {month}')
    return payload


def fetch_detail_description(event):
    event_path = clean_text(event.get('fullUrl'))
    if not event_path:
        return ''
    response = requests.get(
        requests.compat.urljoin(SOURCE_URL, event_path),
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('.eventitem-column-content'))


class MatthewToogoodComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='matthewtoogood_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        # The calendar was created in 2022. Query month-by-month through three
        # future years so the archive and unusually early announcements are
        # both covered; the endpoint returns one complete month per request.
        months = [
            f'{month:02d}-{year}'
            for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 4)
            for month in range(1, 13)
        ]
        events = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_month, month): month for month in months}
            for future in as_completed(futures):
                month = futures[future]
                try:
                    events.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch calendar month',
                        event='crawler_page_failed',
                        level='warning',
                        url=CALENDAR_API,
                        month=month,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        if not events:
            raise RuntimeError('Calendar API returned no events for the archive range')

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_detail_description, event): event
                for event in events
                if event.get('fullUrl')
                and clean_text((event.get('location') or {}).get('addressTitle'))
                and resolve_country(event)
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    event['detail_description'] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch calendar event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=requests.compat.urljoin(SOURCE_URL, event.get('fullUrl', '')),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = [record for event in events if (record := make_record(event))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MatthewToogoodComCrawler().run()


if __name__ == '__main__':
    main()
