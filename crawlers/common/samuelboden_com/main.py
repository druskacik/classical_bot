import calendar
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.samuelboden.com/performance-diary/'
SOURCE = 'Samuel Boden'
AJAX_URL = 'https://www.samuelboden.com/wp-admin/admin-ajax.php'
CALENDAR_ID = '123'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Referer': SOURCE_URL,
    'X-Requested-With': 'XMLHttpRequest',
}

COUNTRIES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'canada': 'CA',
    'england': 'GB', 'finland': 'FI', 'france': 'FR', 'germany': 'DE',
    'hungary': 'HU', 'italy': 'IT', 'luxembourg': 'LU', 'netherlands': 'NL',
    'norway': 'NO', 'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'uk': 'GB', 'united kingdom': 'GB', 'usa': 'US', 'united states': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def external_url(description):
    for link in description.select('a[href]') if description else []:
        url = link.get('href', '').strip()
        if 'google.com/url' in url:
            url = parse_qs(urlparse(url).query).get('q', [url])[0]
        if url.startswith('http') and 'samuelboden.com' not in url:
            return url
    return SOURCE_URL


def parse_country(address):
    lowered = address.lower().rstrip(' .')
    for label, code in COUNTRIES.items():
        if re.search(rf'(?:,|\s)\s*{re.escape(label)}$', lowered):
            return code
    if re.search(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', address, re.I):
        return 'GB'
    if re.search(r'\b[A-Z]{2}\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d\b', address):
        return 'CA'
    if re.search(r'\b[A-Z]{2}\s+\d{5}\b', address):
        return 'US'
    if re.search(r'\bAdelaide\b', address, re.I):
        return 'AU'
    if re.search(r'\bTrondheim\b', address, re.I):
        return 'NO'
    return None


def parse_city(address, country_code):
    flat = re.sub(r'\s+', ' ', address).strip()
    known_city_patterns = (
        r'\b(Budapest)\b',
        r'\b(Quebec City)\b',
    )
    for pattern in known_city_patterns:
        match = re.search(pattern, flat, re.I)
        if match:
            return match.group(1)

    if country_code == 'SE':
        match = re.search(r'\b\d{3}\s+\d{2}\s+([^,]+)', flat)
        if match:
            return match.group(1).strip()
    patterns = {
        'GB': r'\b([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .’\'-]+?)\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b',
        'US': r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}\b',
        'CA': r',\s*([^,]+),\s*[A-Z]{2}\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d\b',
        'AU': r',\s*([^,]+)\s+[A-Z]{2,3}\s+\d{4}\b',
    }
    if country_code in patterns:
        matches = re.findall(patterns[country_code], flat, re.I)
        if matches:
            return matches[-1].strip(' ,')

    # Most continental addresses put the locality immediately after the postcode.
    matches = re.findall(r'\b\d{4,5}(?:\s+[A-Z]{2})?\s+([^,]+)', flat)
    if matches:
        city = re.sub(r'\s+(?:MI|SI|BT)$', '', matches[-1], flags=re.I).strip()
        if city:
            return city

    parts = [part.strip() for part in re.split(r'[,\n]', address) if part.strip()]
    if country_code == 'LU' and parts:
        return 'Luxembourg'
    if len(parts) >= 2:
        candidate = re.sub(r'\b(?:UK|England|Australia|Norway)\b$', '', parts[-2], flags=re.I)
        candidate = re.sub(r'^\d{4,5}\s+', '', candidate).strip()
        if candidate and not re.search(r'\d', candidate):
            return candidate
    return None


def parse_event(event):
    title = clean_text(event.select_one('.simcal-event-title'))
    start = event.select_one('[itemprop="startDate"][content]')
    location = event.select_one('.simcal-event-address')
    description_element = event.select_one('.simcal-event-description')
    address = clean_text(location)
    if not title or start is None or not address:
        return None

    try:
        starts_at = datetime.fromisoformat(start['content'])
    except (KeyError, ValueError):
        return None

    venue = re.split(r'[,\n]', address, maxsplit=1)[0].strip()
    # An address by itself is not a defensible venue name.
    if (
        not venue
        or re.match(r'^\d+\s', venue)
        or re.match(
            r'^(?:Rue|Route|Desguinlei|Kongsgårsgata)\b', venue, re.I
        )
    ):
        return None
    country_code = parse_country(address)
    city = parse_city(address, country_code)
    if not country_code or not city:
        return None

    description = clean_text(description_element) or None
    return {
        'title': re.sub(r'\s+\(\d+\)$', '', title),
        'date': starts_at.date().isoformat(),
        'url': external_url(description_element),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SamuelbodenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='samuelboden_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
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
            response = session.get(SOURCE_URL, timeout=45)
            response.raise_for_status()
            page = BeautifulSoup(response.text, 'html.parser')
            calendar_element = page.select_one(
                f'.simcal-calendar[data-calendar-id="{CALENDAR_ID}"]'
            )
            if calendar_element is None:
                raise ValueError('Performance calendar was not found')
            first = datetime.fromtimestamp(int(calendar_element['data-events-first']))
            last = datetime.fromtimestamp(int(calendar_element['data-events-last']))

            records = []
            year, month = first.year, first.month
            while (year, month) <= (last.year, last.month):
                api_response = session.post(
                    AJAX_URL,
                    data={
                        'action': 'simcal_default_calendar_draw_grid',
                        'month': month,
                        'year': year,
                        'id': CALENDAR_ID,
                    },
                    timeout=45,
                )
                api_response.raise_for_status()
                payload = api_response.json()
                if not payload.get('success') or not isinstance(payload.get('data'), str):
                    raise ValueError(f'Invalid calendar response for {year}-{month:02d}')
                month_soup = BeautifulSoup(payload['data'], 'html.parser')
                for event in month_soup.select('li.simcal-event'):
                    record = parse_event(event)
                    if record:
                        records.append(record)
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        except (requests.RequestException, ValueError, KeyError) as error:
            log_message(
                'Failed to fetch Samuel Boden performance diary',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'], record['title'], record['venue']
            ),
        )


def main():
    SamuelbodenComCrawler().run()


if __name__ == '__main__':
    main()
