import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operacarlofelicegenova.it/'
CALENDAR_URL = f'{SOURCE_URL}spettacoli/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Teatro Carlo Felice Genova'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

HOME_VENUE_MARKERS = (
    'teatro carlo felice',
    'teatro auditorium eugenio montale',
    'teatro della gioventù',
    'marina genova',
)

CITY_SUFFIXES = {
    'auditorium acquario di genova': 'Genova',
    'basilica santissima annunziata del vastato di genova': 'Genova',
    'piazzetta di portofino': 'Portofino',
    'teatro cavour imperia': 'Imperia',
    'teatro comunale di ventimiglia': 'Ventimiglia',
    'teatro sociale di camogli': 'Camogli',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def filter_values(soup, filter_id):
    return [
        field['value']
        for field in soup.select(f'#dkrsm-filters-{filter_id} input[value]')
    ]


def ajax_nonce(soup):
    match = re.search(r'"dkrsm_ajax_nonce":"([^"]+)"', str(soup))
    return match.group(1) if match else None


def fetch_feed(session, filter_id, values, nonce):
    data = [('action', 'dkrsm_ajax_filter')]
    data.extend(('dkrsm_passed_values[]', value) for value in values)
    data.append(('dkrsm_ajax_nonce', nonce))
    response = session.post(AJAX_URL, data=data, timeout=60)
    response.raise_for_status()
    payload = response.json()
    return list((payload.get('dkrsm-data') or {}).values())


def city_from_venue(venue):
    normalized = clean_text(venue)
    folded = normalized.casefold()
    if any(marker in folded for marker in HOME_VENUE_MARKERS):
        return 'Genova'
    folded_without_province = re.sub(r'\s*\([a-z]{2}\)\s*$', '', folded).strip()
    if folded_without_province in CITY_SUFFIXES:
        return CITY_SUFFIXES[folded_without_province]

    if ',' in normalized:
        city = normalized.rsplit(',', 1)[1].strip()
        city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city).strip()
        if city:
            return city
    return None


def parse_occurrences(soup, fallback_date):
    occurrences = []
    for row in soup.select('table tr'):
        date_node = row.select_one('.dkr-calendar-table-date')
        time_node = row.select_one('.dkr-calendar-table-time')
        if date_node is None:
            continue
        date_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', clean_text(date_node))
        if not date_match:
            continue
        try:
            event_date = datetime.strptime(date_match.group(1), '%d/%m/%Y').date().isoformat()
        except ValueError:
            continue
        time_match = re.search(r'\b(\d{1,2})[:.]([0-5]\d)\b', clean_text(time_node))
        time_from = None
        if time_match and 0 <= int(time_match.group(1)) <= 23:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        occurrences.append((event_date, time_from))

    if occurrences:
        return occurrences
    try:
        parsed = datetime.strptime(fallback_date, '%a %d %B %Y %H:%M:%S')
    except (TypeError, ValueError):
        return []
    return [(parsed.date().isoformat(), parsed.strftime('%H:%M'))]


def detail_description(soup, excerpt):
    parts = [clean_text(excerpt)]
    content = soup.select_one('.elementor-widget-theme-post-content .elementor-widget-container')
    content_text = clean_text(content)
    if content_text and content_text not in parts:
        parts.append(content_text)
    return clean_text('\n\n'.join(part for part in parts if part)) or None


def detail_title(soup, fallback):
    node = soup.select_one('h1.elementor-heading-title')
    return clean_text(node) or clean_text(BeautifulSoup(str(fallback), 'html.parser'))


class OperaCarloFeliceGenovaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operacarlofelicegenova_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
            calendar = get_page(session, CALENDAR_URL)
            nonce = ajax_nonce(calendar)
            if not nonce:
                raise ValueError('AJAX nonce was not found')
            feed = []
            for filter_id in ('shows-next', 'shows-previous'):
                values = filter_values(calendar, filter_id)
                if not values:
                    raise ValueError(f'No filter values found for {filter_id}')
                feed.extend(fetch_feed(session, filter_id, values, nonce))
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            log_message(
                'Failed to fetch Teatro Carlo Felice calendar feed',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        seen_urls = set()
        for item in feed:
            url = item.get('permalink')
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            venue = clean_text(item.get('location'))
            city = city_from_venue(venue)
            if not venue or not city:
                log_message(
                    'Skipping Teatro Carlo Felice event with unresolved location',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
                continue
            try:
                detail = get_page(session, url)
                title = detail_title(detail, item.get('show_title'))
                description = detail_description(detail, item.get('show_excerpt'))
                occurrences = parse_occurrences(detail, item.get('date'))
                for event_date, time_from in occurrences:
                    if title:
                        records.append({
                            'title': title,
                            'date': event_date,
                            'url': url,
                            'time_from': time_from,
                            'venue': venue,
                            'city': city,
                            'country_code': 'IT',
                            'description': description,
                            'source_url': SOURCE_URL,
                            'source': SOURCE,
                        })
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro Carlo Felice event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    OperaCarloFeliceGenovaItCrawler().run()


if __name__ == '__main__':
    main()
