import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fondazionecantiere.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/eventi'
SOURCE = "Fondazione Cantiere d'Arte Montepulciano"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_year(url, date_text):
    match = re.search(r'/(?:[^/]+-)?(20\d{2})/', url)
    if match:
        return int(match.group(1))
    match = re.search(r'\b(20\d{2})\b', date_text)
    return int(match.group(1)) if match else None


def parse_date(date_text, url):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(20\d{2}))?\b',
        date_text.casefold(),
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else event_year(url, date_text)
    if year is None:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(text):
    parts = re.split(r'\s*[\-–—]\s*', clean_text(text), maxsplit=1)
    if len(parts) != 2:
        return None
    city, venue = (part.strip(' ,') for part in parts)
    if not city or not venue:
        return None
    return city.title(), venue


def parse_time(text):
    match = re.search(r'\b(?:ore\s*)?(\d{1,2})[:.]([0-5]\d)\b', clean_text(text), re.I)
    if not match or int(match.group(1)) > 23:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def parse_event(item, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.titoloevento'))
    location = parse_location(soup.select_one('.luogoeve'))
    event_date = parse_date(clean_text(soup.select_one('.dataeve')), item['link'])
    if not title or not location or not event_date:
        return None

    content = BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser')
    for unwanted in content.select('script, style, form, .wp-block-buttons, .pulsanteeventosingolo'):
        unwanted.decompose()
    description = clean_text(content) or None
    city, venue = location
    return {
        'title': title,
        'date': event_date,
        'url': item['link'],
        'time_from': parse_time(soup.select_one('.oraeve')),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FondazioneCantiereItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fondazionecantiere_it',
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
            items = []
            page = 1
            while True:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        '_fields': 'id,link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                items.extend(response.json())
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                if page >= total_pages:
                    break
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Fondazione Cantiere event API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, item['link'], timeout=45): item
                for item in items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    detail = future.result()
                    detail.raise_for_status()
                    record = parse_event(item, detail.text)
                    if record is None:
                        retry = session.get(item['link'], timeout=45)
                        retry.raise_for_status()
                        record = parse_event(item, retry.text)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped Fondazione Cantiere event with incomplete details',
                            event='crawler_item_skipped',
                            level='warning',
                            url=item['link'],
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Fondazione Cantiere event',
                        event='crawler_item_failed',
                        level='warning',
                        url=item['link'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FondazioneCantiereItCrawler().run()


if __name__ == '__main__':
    main()
