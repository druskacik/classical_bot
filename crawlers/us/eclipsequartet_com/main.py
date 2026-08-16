import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://eclipsequartet.com/'
SOURCE = 'Eclipse Quartet'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
FEEDS = (
    ('upcoming-concerts', 1246, 1250),
    ('past-concerts', 1245, 1258),
)
US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
        if '<' in raw
        else raw.strip()
    )
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(row):
    # A date range represents a residency/overview, not a concrete occurrence.
    if len(row.select('.event-date')) != 1:
        return ''
    month = clean_text(row.select_one('.event-month'))
    day = clean_text(row.select_one('.event-date'))
    year = clean_text(row.select_one('.event-year'))
    try:
        return datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value)
    if not value:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            parsed = datetime.strptime(value, pattern)
            # Midnight is the site's unset/default value, not an advertised time.
            if parsed.hour == 0 and parsed.minute == 0:
                return None
            return parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_city(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if parts and parts[-1].casefold() in {'united states', 'usa', 'us'}:
        parts.pop()
    for index in range(1, len(parts)):
        state = re.sub(r'\s+\d{5}(?:-\d{4})?$', '', parts[index]).upper()
        if state in US_STATES or parts[index].casefold() == 'california':
            later_has_state = any(
                re.sub(r'\s+\d{5}(?:-\d{4})?$', '', part).upper() in US_STATES
                or part.casefold() == 'california'
                for part in parts[index + 1:]
            )
            if index + 1 < len(parts) and not later_has_state:
                return parts[-1]
            return parts[index - 1]
    return ''


def strip_location_suffix(value, city):
    venue = clean_text(value)
    if not venue or not city:
        return ''
    parts = [part.strip() for part in venue.split(',') if part.strip()]
    city_indexes = [i for i, part in enumerate(parts) if part.casefold() == city.casefold()]
    if city_indexes:
        parts = parts[:city_indexes[0]]
    parts = [
        part for part in parts
        if re.sub(r'\s+\d{5}(?:-\d{4})?$', '', part).upper() not in US_STATES
        and part.casefold() not in {'california', 'united states', 'usa', 'us'}
    ]
    venue = ', '.join(parts).strip(' ,')
    if venue.casefold() in {city.casefold(), 'tbd', 'to be determined'}:
        return ''
    return venue


def detail_fields(session, url, fallback_location):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    values = [clean_text(item) for item in soup.select('.event-small-info')]

    time_from = next((parse_time(value) for value in values if parse_time(value)), None)
    location_values = [
        value for value in values
        if not re.search(r'\d{1,2}/\d{1,2}/\d{4}', value)
        and not re.fullmatch(r'\d{1,2}(?::\d{2})?\s*[AP]M', value, re.I)
    ]
    combined = ', '.join(location_values) or fallback_location
    city = parse_city(combined) or parse_city(fallback_location)
    venue_source = location_values[0] if location_values else fallback_location
    venue = strip_location_suffix(venue_source, city)
    if not venue:
        venue = strip_location_suffix(fallback_location, city)
    return time_from, venue, city


def parse_row(session, row):
    link = row.select_one('.event-name a[href]')
    title = clean_text(link)
    url = clean_text(link.get('href')) if link else ''
    event_date = parse_date(row)
    description = clean_text(row.select_one('.event-group')) or None
    fallback_location = clean_text(row.select_one('.event-location'))

    try:
        time_from, venue, city = detail_fields(session, url, fallback_location)
    except requests.RequestException as error:
        log_message(
            'Could not load Eclipse Quartet event detail; using listing data',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        time_from = None
        city = parse_city(fallback_location)
        venue = strip_location_suffix(fallback_location, city)

    if not all((title, event_date, url, venue, city)):
        log_message(
            'Skipped incomplete Eclipse Quartet event',
            event='crawler_item_skipped',
            level='warning',
            url=url or SOURCE_URL,
            error_type='IncompleteEventData',
            error_message='Required title, date, URL, venue, or city is missing',
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


class EclipseQuartetComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eclipsequartet_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def fetch_page(self, session, slug, view_id, post_id, page):
        response = session.post(
            AJAX_URL,
            data={
                'view_number': view_id,
                'page': page,
                'environment[wpv_aux_current_post_id]': post_id,
                'environment[wpv_aux_parent_post_id]': post_id,
                'expect': 'full',
                'action': 'wpv_get_view_query_results',
                'id': view_id,
                'wpv_view_widget_id': 0,
            },
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': f'{SOURCE_URL}{slug}/',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get('success'):
            raise ValueError(f'WordPress view {view_id} returned an unsuccessful response')
        soup = BeautifulSoup(payload['data']['full'], 'html.parser')
        pagination = soup.select_one('[data-pagination]')
        metadata = json.loads(pagination.get('data-pagination', '{}')) if pagination else {}
        return soup.select('.events-tr'), int(metadata.get('max_pages') or 1)

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        seen_urls = set()

        for slug, view_id, post_id in FEEDS:
            page = 1
            max_pages = 1
            while page <= max_pages:
                rows, discovered_pages = self.fetch_page(
                    session, slug, view_id, post_id, page
                )
                max_pages = max(max_pages, discovered_pages)
                for row in rows:
                    link = row.select_one('.event-name a[href]')
                    url = clean_text(link.get('href')) if link else ''
                    if url in seen_urls:
                        continue
                    record = parse_row(session, row)
                    if record:
                        records.append(record)
                        seen_urls.add(url)
                page += 1

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    EclipseQuartetComCrawler().run()


if __name__ == '__main__':
    main()
