import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaiasi.ro/'
CALENDAR_API = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Opera Națională Română Iași'
CITY = 'Iași'
DEFAULT_VENUE = 'Opera Națională Română Iași'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_events(session):
    # The endpoint applies `start` but has historically ignored `end`. Asking
    # from well before the first published event therefore returns the entire
    # retained archive as well as all announced performances.
    params = {
        'action': 'WP_FullCalendar',
        'type': 'ticketsys-event',
        'month': 1,
        'year': 2010,
        'start': '2010-01-01',
        'end': f'{date.today().year + 5}-12-31',
    }
    response = session.get(CALENDAR_API, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def detail_record(session, item):
    title = clean_text(item.get('title'))
    url = (item.get('url') or '').strip()
    start = item.get('start') or ''
    try:
        start_value = date.fromisoformat(start[:10]).isoformat()
    except (TypeError, ValueError):
        return None
    time_match = re.search(r'T(\d{2}:\d{2})', start)
    if not title or not url:
        return None

    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    article = soup.select_one('article.ticketsys-event')
    venue_node = soup.select_one('.ei-venue')
    venue = clean_text(venue_node.get_text(' ', strip=True)) if venue_node else ''
    if not article:
        return None

    # TicketSys removes its venue widget after some archived performances.
    # This is the opera's own performance calendar and almost all entries are
    # at its home theatre; two recurring, explicitly labelled local series use
    # other defensible venues.
    if not venue:
        if re.search(r'\bpalat(?:ul|ului)?\b', title, re.IGNORECASE):
            venue = 'Palatul Culturii din Iași'
        elif re.search(r'\bfoaier\b', title, re.IGNORECASE):
            venue = 'Foaierul Operei Naționale Române Iași'
        else:
            venue = DEFAULT_VENUE

    # The calendar belongs to the Iași opera and its alternate local venues
    # (the Palace, foyer, etc.) are still in Iași. Do not assign this default
    # when a detail page explicitly names a different city.
    explicit_other_city = re.search(
        r'\b(București|Cluj(?:-Napoca)?|Timișoara|Brașov|Sibiu|Suceava|Bacău)\b',
        venue,
        re.IGNORECASE,
    )
    if explicit_other_city:
        city = explicit_other_city.group(1)
    else:
        city = CITY

    description_node = BeautifulSoup(str(article), 'html.parser')
    for node in description_node.select(
        '#select-tickets-div, #venue, #ticketsCont, #ticketsContMobile, '
        '#dialogLoginForm, script, style, img, input, button'
    ):
        node.decompose()
    description = clean_text(description_node)

    return {
        'title': title,
        'date': start_value,
        'url': url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'RO',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OperaIasiRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaiasi_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = calendar_events(session)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(detail_record, session, item): item for item in items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=item.get('url'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['url']),
        )


def main():
    OperaIasiRoCrawler().run()


if __name__ == '__main__':
    main()
