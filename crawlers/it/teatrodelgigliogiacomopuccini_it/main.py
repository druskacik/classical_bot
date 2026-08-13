import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatrodelgigliogiacomopuccini.it/it/'
SEARCH_URL = urljoin(SOURCE_URL, 'il-calendario/ricerca/')
SOURCE = 'Teatro del Giglio Giacomo Puccini'

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

# The calendar is predominantly for Lucca, but it also includes explicitly
# labelled performances elsewhere in the province.  These labels come from the
# site's first-party location filter.
VENUE_CITIES = {
    'Chiesa Don Bosco (Viareggio)': 'Viareggio',
    'Chiesa di Capezzano Pianore (Camaiore)': 'Camaiore',
    'Chiesa di San Francesco (Borgo a Mozzano)': 'Borgo a Mozzano',
    'Teatro I. Nieri (Ponte a Moriano)': 'Lucca',
    'Vallico di Sopra': 'Fabbriche di Vergemoli',
    'Vergemoli': 'Fabbriche di Vergemoli',
    'Montecarlo': 'Montecarlo',
    'Fornovolasco, ingresso Grotte del Vento': 'Fabbriche di Vergemoli',
    'San Romano di Garfagnana': 'San Romano in Garfagnana',
    'Celle dei Puccini': 'Pescaglia',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([a-zà]+)\s+(\d{4})\b', value.casefold())
    if not match:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def city_for_venue(venue):
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    parenthetical = re.search(r'\(([^()]+)\)\s*$', venue)
    if parenthetical and parenthetical.group(1).casefold() not in {'lucca'}:
        return parenthetical.group(1).strip()
    return 'Lucca'


def search_form(session):
    response = session.get(SEARCH_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    form = soup.select_one('form[action*="/il-calendario/ricerca/risultati/"]')
    if form is None:
        raise ValueError('Calendar search form was not found')
    fields = {
        node['name']: node.get('value', '')
        for node in form.select('input[type="hidden"][name]')
        if not node['name'].endswith('[]')
    }
    return urljoin(SEARCH_URL, form.get('action', '')), fields


def result_page(session, action, base_fields, page_number):
    fields = dict(base_fields)
    fields.update({
        'tx_calendarize_calendar[currentPage]': str(page_number),
        'tx_calendarize_calendar[startDate]': '2021-01-01',
        'tx_calendarize_calendar[endDate]': f'{date.today().year + 2}-12-31',
        'tx_calendarize_calendar[customSearch][fullText]': '',
    })
    response = session.post(action, data=fields, timeout=90)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def detail_description(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    main = soup.select_one('main.main-content')
    description = clean_text(main) or None
    return description


def parse_card(card):
    title_node = card.select_one('.event-card__title a[href]')
    topbar = card.select_one('.event-card__topbar')
    if title_node is None or topbar is None:
        return None

    direct_spans = topbar.find_all('span', recursive=False)
    venue = clean_text(direct_spans[0]) if direct_spans else ''
    title = clean_text(title_node)
    url = urljoin(SOURCE_URL, title_node.get('href', ''))
    event_date = parse_date(clean_text(topbar))
    time_from = parse_time(clean_text(topbar))
    if not title or not event_date or not url or not venue or venue.casefold() == 'da definire':
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'IT',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TeatroDelGiglioGiacomoPucciniItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrodelgigliogiacomopuccini_it',
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
            action, fields = search_form(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to load Teatro del Giglio calendar search',
                event='crawler_fetch_failed',
                level='error',
                url=SEARCH_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        seen_page_signatures = set()
        page_number = 1
        while True:
            try:
                soup = result_page(session, action, fields, page_number)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro del Giglio calendar page',
                    event='crawler_fetch_failed',
                    level='error',
                    url=action,
                    page_number=page_number,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            cards = soup.select('main.main-content .event-card')
            signature = tuple(clean_text(card.select_one('.event-card__topbar')) for card in cards)
            if not cards or signature in seen_page_signatures:
                break
            seen_page_signatures.add(signature)

            for card in cards:
                item_url = action
                try:
                    record = parse_card(card)
                    if record:
                        item_url = record['url']
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatro del Giglio event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=item_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

            page_number += 1

        descriptions = {}
        urls = {record['url'] for record in records}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(detail_description, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatro del Giglio event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    descriptions[url] = None
        for record in records:
            record['description'] = descriptions.get(record['url'])

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    TeatroDelGiglioGiacomoPucciniItCrawler().run()


if __name__ == '__main__':
    main()
