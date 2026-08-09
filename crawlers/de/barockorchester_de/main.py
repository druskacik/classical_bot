import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://barockorchester.de/'
SOURCE = 'Freiburger Barockorchester'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'Belgien': 'BE',
    'Dänemark': 'DK',
    'Deutschland': 'DE',
    'Frankreich': 'FR',
    'Großbritannien': 'GB',
    'Italien': 'IT',
    'Luxembourg': 'LU',
    'Luxemburg': 'LU',
    'Niederlande': 'NL',
    'Österreich': 'AT',
    'Polen': 'PL',
    'Schweiz': 'CH',
    'Spanien': 'ES',
}

EVENT_FIELDS = 'id,link,title,ort'
DETAIL_WORKERS = 8


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b', value)
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_detail(page_html, url, country_code):
    soup = BeautifulSoup(page_html, 'html.parser')
    entry = soup.select_one('.fbo-kalendereintrag .container-aussen')
    if entry is None:
        return None

    title = clean_text(entry.select_one('h1'))
    heading_row = entry.select_one('.row.pt-2125')
    detail_text = clean_text(heading_row.select_one('.lc-block:not(.pb-100)')) if heading_row else ''
    event_date = parse_date(detail_text)
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*Uhr\b', detail_text)

    # The site consistently renders: weekday/date, time, city, venue. Venue
    # names can themselves contain commas, so only the first three separators
    # are structural.
    parts = [part.strip() for part in detail_text.split(',')]
    location_index = 2 if len(parts) > 1 and 'Uhr' in parts[1] else 1
    city = parts[location_index] if len(parts) > location_index + 1 else ''
    venue = ', '.join(parts[location_index + 1:]).strip()

    description_element = entry.select_one('.col-lg-6.ps-lg-4 .ul-style')
    description = clean_text(description_element) or None
    if not all((title, event_date, url, venue, city, country_code)):
        return None

    return {
        'title': html.unescape(title),
        'date': event_date,
        'url': url,
        'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BarockorchesterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='barockorchester_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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

    def _get_json(self, path, params=None):
        response = requests.get(
            f'{API_URL}/{path}', params=params, headers=HEADERS, timeout=45
        )
        response.raise_for_status()
        return response.json(), response.headers

    def _location_countries(self):
        terms, _ = self._get_json('ort', {'per_page': 100, 'hide_empty': 'false'})
        by_id = {term['id']: term for term in terms}
        result = {}
        for term_id, term in by_id.items():
            parent = by_id.get(term.get('parent'))
            country_name = parent['name'] if parent else term['name']
            country_code = COUNTRY_CODES.get(html.unescape(country_name))
            if country_code and parent:
                result[term_id] = country_code
        return result

    def _events(self):
        params = {'per_page': 100, '_fields': EVENT_FIELDS, 'page': 1}
        first_page, headers = self._get_json('konzertkalender', params)
        events = list(first_page)
        total_pages = int(headers.get('X-WP-TotalPages', '1'))
        for page_number in range(2, total_pages + 1):
            params['page'] = page_number
            page, _ = self._get_json('konzertkalender', params)
            events.extend(page)
        return events

    def _fetch_event(self, event, location_countries):
        country_codes = {
            location_countries[term_id]
            for term_id in event.get('ort', [])
            if term_id in location_countries
        }
        if len(country_codes) != 1:
            return None

        url = event.get('link', '')
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Freiburger Barockorchester concert',
                event='crawler_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None
        return parse_detail(response.text, url, country_codes.pop())

    def scrape(self):
        try:
            location_countries = self._location_countries()
            events = self._events()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Freiburger Barockorchester API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
            futures = [
                executor.submit(self._fetch_event, event, location_countries)
                for event in events
            ]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BarockorchesterDeCrawler().run()


if __name__ == '__main__':
    main()
