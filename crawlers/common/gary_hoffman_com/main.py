import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://www.gary-hoffman.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'Gary Hoffman'
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
FIRST_ARCHIVE_YEAR = 2018
FUTURE_MONTHS = 18

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRIES = {
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'china': 'CN',
    'croatia': 'HR',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'denmark': 'DK',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'hungary': 'HU',
    'israel': 'IL',
    'italy': 'IT',
    'japan': 'JP',
    'luxembourg': 'LU',
    'netherlands': 'NL',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'romania': 'RO',
    'slovakia': 'SK',
    'slovenia': 'SI',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'united kingdom': 'GB',
    'uk': 'GB',
    'united states': 'US',
    'usa': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_pairs():
    today = date.today()
    final_index = today.year * 12 + today.month - 1 + FUTURE_MONTHS
    first_index = FIRST_ARCHIVE_YEAR * 12
    return [(index // 12, index % 12 + 1) for index in range(first_index, final_index + 1)]


def ajax_payload(base, shortcode, year, month):
    # EventON's switch-month action returns the month following cmonth.
    previous_index = year * 12 + month - 2
    previous_year, previous_month = previous_index // 12, previous_index % 12 + 1
    payload = {
        'action': 'the_ajax_hook',
        'direction': 'next',
        'filters': '',
        'ajaxtype': 'switchmonth',
    }
    shortcode = {**shortcode, 'hide_past': 'no', 'number_of_months': '1'}
    base = {**base, 'cyear': str(previous_year), 'cmonth': str(previous_month)}
    payload.update({f'shortcode[{key}]': value for key, value in shortcode.items()})
    payload.update({f'evodata[{key}]': value for key, value in base.items()})
    return payload


def extract_country(text):
    normalized = re.sub(r'\s+', ' ', text).strip(' ,').lower()
    for name, code in sorted(COUNTRIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf'(?:^|[, ]){re.escape(name)}$', normalized):
            return code, name
    return None, None


def extract_location(event):
    location = event.select_one('.evo_location')
    if location is None:
        return None
    name = clean_text(location.select_one('.evo_location_name')).strip(' ,')
    address = clean_text(location.select_one('.evo_location_address')).strip(' ,')
    combined = ', '.join(part for part in (name, address) if part)
    country_code, country_name = extract_country(combined)
    if not country_code:
        return None

    address_without_country = re.sub(
        rf'(?i),?\s*{re.escape(country_name)}\s*$', '', address
    ).strip(' ,')
    address_parts = [part.strip() for part in address_without_country.split(',') if part.strip()]
    city = ''
    if country_code in {'CA', 'US'}:
        for index, part in enumerate(address_parts):
            if re.search(r'\b[A-Z]{2}\s+[A-Z0-9][A-Z0-9 -]{3,9}$', part, re.I):
                city = address_parts[index - 1] if index else ''
                break
    if not city:
        for part in address_parts:
            postal_city = re.match(r'^(?:[A-Z]{1,3}-)?\d{4,6}\s+(.+)$', part)
            if postal_city:
                city = postal_city.group(1).strip()
                break
    if not city and address_parts:
        candidate = address_parts[-1]
        if re.search(r'\d', candidate) and len(address_parts) > 1:
            candidate = address_parts[-2]
        city = candidate.strip()

    name_without_country = re.sub(
        rf'(?i),?\s*{re.escape(country_name)}\s*$', '', name
    ).strip(' ,')
    name_parts = [part.strip() for part in name_without_country.split(',') if part.strip()]
    if not city and len(name_parts) >= 2:
        city = name_parts[-1]

    # A location consisting only of "City, Country" does not provide a venue.
    venue = name_without_country
    if not venue or (len(name_parts) == 1 and not address_parts):
        return None
    if city and venue.casefold() == city.casefold():
        return None
    if not city:
        return None
    return venue, city, country_code


def extract_time(event):
    value = clean_text(event.select_one('.evo_start .time'))
    if not value or 'all day' in value.lower():
        return None
    match = re.search(r'\b([01]?\d|2[0-3])\s*(?::|h)\s*([0-5]\d)\b', value)
    if not match:
        return None
    parsed = f'{int(match.group(1)):02d}:{match.group(2)}'
    return None if parsed == '00:00' else parsed


def parse_event(event):
    title = clean_text(event.select_one('.evcal_event_title'))
    schema = event.select_one('.evo_event_schema')
    url_element = schema.select_one('[itemprop="url"][href]') if schema else None
    date_element = schema.select_one('[itemprop="startDate"]') if schema else None
    raw_date = date_element.get('content', '') if date_element else ''
    match = re.match(r'(20\d{2})-(\d{1,2})-(\d{1,2})', raw_date)
    location = extract_location(event)
    if not title or not url_element or not match or not location:
        return None
    try:
        event_date = date(*(int(value) for value in match.groups())).isoformat()
    except ValueError:
        return None

    venue, city, country_code = location
    description = clean_text(event.select_one('.eventon_desc_in'))
    if not description and schema:
        description_meta = schema.select_one('meta[itemprop="description"]')
        description = description_meta.get('content', '').strip() if description_meta else ''
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, url_element['href']),
        'time_from': extract_time(event),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class GaryHoffmanComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gary_hoffman_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Gary Hoffman calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        data = soup.select_one('.evocal_data')
        if data is None:
            raise ValueError('EventON calendar configuration was not found')
        base = json.loads(data['data-base'])
        shortcode = json.loads(data['data-sc'])

        def fetch_month(year_month):
            year, month = year_month
            month_session = requests.Session()
            month_session.headers.update({**HEADERS, 'X-Requested-With': 'XMLHttpRequest'})
            response = month_session.post(
                AJAX_URL,
                data=ajax_payload(base, shortcode, year, month),
                timeout=45,
            )
            response.raise_for_status()
            result = response.json()
            if result.get('status') != 'GOOD':
                raise ValueError(f'Unexpected EventON response for {year}-{month:02d}')
            return result.get('content', '')

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_month, pair): pair for pair in month_pairs()}
            for future in as_completed(futures):
                year, month = futures[future]
                try:
                    content = future.result()
                except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                    log_message(
                        'Failed to fetch Gary Hoffman calendar month',
                        event='crawler_fetch_failed',
                        level='warning',
                        url=AJAX_URL,
                        year=year,
                        month=month,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                month_soup = BeautifulSoup(content, 'html.parser')
                for event in month_soup.select('.eventon_list_event[id^="event_"]'):
                    record = parse_event(event)
                    if record:
                        records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    GaryHoffmanComCrawler().run()


if __name__ == '__main__':
    main()
