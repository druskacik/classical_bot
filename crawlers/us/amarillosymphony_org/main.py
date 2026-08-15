import html
import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.amarillosymphony.org/'
SOURCE = 'Amarillo Symphony'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

SEASON_CATEGORY_RE = re.compile(
    r'(?:\bsymphony season\b|\bchamber music amarillo\b)', re.IGNORECASE
)
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )
    )
    if name
}


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    if '<' in value and '>' in value:
        value = BeautifulSoup(value, 'html.parser').get_text(' ', strip=True)
    text = html.unescape(value)
    return re.sub(r'\s+', ' ', text).strip()


def clean_venue(value):
    """Remove legacy address text embedded in The Events Calendar venue names."""
    value = clean_text(value)
    value = re.split(r',\s*\d{2,}\b|\s+\d{2,}\s+[NSEW]?\s*\w+', value, maxsplit=1)[0]
    return value.strip(' ,')


def parse_api_datetime(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def explicit_schedule(description, fallback_year):
    """Extract explicitly listed Month Day / time occurrences from event copy."""
    pattern = re.compile(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
        r'(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+)?'
        r'(\d{1,2})\s*(?://|at)\s*(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?',
        re.IGNORECASE,
    )
    occurrences = []
    for month_name, day, hour, minute, meridiem in pattern.findall(description):
        month = MONTHS.get(month_name.lower()) if month_name else None
        if not month:
            continue
        hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
        try:
            event_date = date(fallback_year, month, int(day)).isoformat()
        except ValueError:
            continue
        occurrences.append((event_date, f'{hour:02d}:{minute or "00"}'))
    return list(dict.fromkeys(occurrences))


def event_occurrences(event, description):
    start = parse_api_datetime(event.get('start_date'))
    end = parse_api_datetime(event.get('end_date'))
    if not start:
        return []

    schedule = explicit_schedule(description, start.year)
    if schedule:
        return schedule

    last_date = end.date() if end and end.date() >= start.date() else start.date()
    if (last_date - start.date()).days > 7:
        last_date = start.date()
    time_from = None if event.get('all_day') or start.time().isoformat() == '00:00:00' else start.strftime('%H:%M')
    occurrences = []
    current = start.date()
    while current <= last_date:
        occurrences.append((current.isoformat(), time_from))
        current += timedelta(days=1)
    return occurrences


def venue_and_city(event):
    venue_data = event.get('venue')
    if isinstance(venue_data, dict):
        venue = clean_venue(venue_data.get('venue'))
        city = clean_text(venue_data.get('city'))
        if venue:
            return venue, city or 'Amarillo'

    category_names = ' '.join(
        clean_text(category.get('name')) for category in event.get('categories', [])
    )
    # The orchestra's regular Symphony Season performances are held at this
    # home hall; special performances have their own API venue records.
    if re.search(r'\bSymphony Season\b', category_names, re.IGNORECASE):
        return 'Globe-News Center for the Performing Arts', 'Amarillo'
    return None, None


class AmarilloSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amarillosymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def _get_json(self, session, path, params=None):
        url = f'{API_URL}/{path}'
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response.json()

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            category_data = self._get_json(session, 'categories', {'per_page': 100})
            category_ids = [
                category['id']
                for category in category_data.get('categories', [])
                if SEASON_CATEGORY_RE.search(clean_text(category.get('name')))
            ]
            if not category_ids:
                log_message(
                    'No Amarillo Symphony season categories found',
                    event='crawler_no_categories',
                    level='warning',
                    url=f'{API_URL}/categories',
                )
                return []

            events = []
            page = 1
            while True:
                payload = self._get_json(
                    session,
                    'events',
                    {
                        'categories': ','.join(map(str, category_ids)),
                        'start_date': '2000-01-01',
                        'end_date': '2100-12-31',
                        'per_page': 50,
                        'page': page,
                    },
                )
                events.extend(payload.get('events', []))
                if page >= int(payload.get('total_pages') or 1):
                    break
                # The API's next_rest_url drops the categories parameter, so
                # construct every page request with the category IDs ourselves.
                page += 1
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Failed to fetch Amarillo Symphony events API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            title = clean_text(event.get('title'))
            url = clean_text(event.get('url'))
            description = clean_text(event.get('description')) or None
            venue, city = venue_and_city(event)
            if not all((title, url, venue, city)):
                log_message(
                    'Skipping event without a defensible venue or city',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url or API_URL,
                )
                continue
            for event_date, time_from in event_occurrences(event, description or ''):
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                })

        log_message(
            'Amarillo Symphony events parsed',
            event='crawler_records_parsed',
            record_count=len(records),
            url=f'{API_URL}/events',
        )
        return records


def main():
    AmarilloSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
