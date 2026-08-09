import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hmtm.de/'
EVENTS_URL = urljoin(SOURCE_URL, 'veranstaltungen/')
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
SOURCE = 'Hochschule für Musik und Theater München'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s*[’\'](\d{2})\s*/\s*'
        r'(\d{1,2}):(\d{2})\s*Uhr',
        value,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(2000 + int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    return event_date, f'{int(match.group(4)):02d}:{match.group(5)}'


def listing_record(item):
    link = item.select_one('.eventlistitem__headline a[href]')
    title_node = item.select_one('.eventlistitem__headline h2')
    subtitle = clean_text(item.select_one('.eventlistitem__subheadline'))
    title = clean_text(title_node)
    if subtitle and subtitle.lower() not in title.lower():
        title = f'{title} – {subtitle}'
    event_date, time_from = parse_datetime(clean_text(item.select_one('.eventlistitem__date')))
    places = item.select('.eventlistitem__place')
    venue = clean_text(places[-1]) if places else ''
    url = urljoin(SOURCE_URL, link.get('href', '')) if link else ''
    if not title or not event_date or not venue or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
    }


def listing_records(session):
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    nonce_match = re.search(r'"nonce"\s*:\s*"([a-f0-9]+)"', response.text)
    if not nonce_match:
        raise ValueError('Could not find event-filter nonce')

    records = []
    page = 1
    while True:
        payload = {
            'action': 'apply_event_filter', 'nonce': nonce_match.group(1),
            'show_prev_pages': 'true', 'paged': page, 'search_term': '',
            'date_from': '', 'date_to': '', 'genre': 0, 'type': 0,
            'place': 0, 'field': 0,
        }
        result = session.post(
            AJAX_URL, data=payload, headers={'X-Requested-With': 'XMLHttpRequest'}, timeout=45
        )
        result.raise_for_status()
        data = result.json()
        soup = BeautifulSoup(data.get('html') or '', 'html.parser')
        page_records = [listing_record(item) for item in soup.select('.eventlistitem')]
        records.extend(record for record in page_records if record)
        if not data.get('has_more'):
            break
        page += 1
    # The endpoint can repeat boundary items (and, under its page cache, whole
    # pages). Preserve the first occurrence before fetching detail pages.
    unique = {}
    for record in records:
        unique.setdefault(record['url'], record)
    return list(unique.values())


def detail_fields(session, record):
    response = session.get(record['url'], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('.single-event .elementor-location-single')
    if content is None:
        content = soup.select_one('.elementor-location-single')
    text = clean_text(content)

    # Detail pages publish full postal addresses, including for external venues.
    city_match = re.search(r'\b\d{5}\s+([^\n]+)', text)
    city = clean_text(city_match.group(1)).strip(' ,') if city_match else ''
    if not city and re.search(r'\b(Arcisstraße|Luisenstraße|Gasteig HP8)\b', record['venue']):
        city = 'München'
    if not city:
        return None

    # Keep the complete event body: it commonly contains programme, composers,
    # works, and participants. Navigation and cookie text live outside this node.
    return city, text or None


class HmtmDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hmtm_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = listing_records(session)
        completed = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(detail_fields, session, record): record for record in records}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    detail = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape HMTM event detail',
                        event='crawler_item_failed', level='warning', url=record['url'],
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if not detail:
                    continue
                city, description = detail
                record.update(
                    city=city, country_code='DE', description=description,
                )
                completed.append(record)
        return sorted(
            completed,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    HmtmDeCrawler().run()


if __name__ == '__main__':
    main()
