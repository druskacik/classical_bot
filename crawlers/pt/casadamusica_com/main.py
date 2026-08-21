import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://casadamusica.com/'
AGENDA_URL = f'{SOURCE_URL}agenda/'
SOURCE = 'Casa da Música'
CITY = 'Porto'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def canonical_event_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def location_from_card(card, time_element):
    date_column = time_element.find_parent('div', class_='flex-col')
    location_column = date_column.find_next_sibling('div') if date_column else None
    lines = [clean_text(line) for line in location_column.find_all('div', recursive=False)] if location_column else []
    lines = [line for line in lines if line]
    venue = lines[0] if lines else ''
    explicit_city = lines[1] if len(lines) > 1 else ''
    card_text = clean_text(card).casefold()
    if not venue and ('nos aliados' in card_text or 'a casa nos aliados' in card_text):
        venue = 'Avenida dos Aliados'
    if explicit_city:
        return venue, explicit_city

    # These are rooms or outdoor stages of Casa da Música in Porto. Do not
    # extend this default to an explicitly named touring venue.
    home_markers = (
        'sala ', 'esplanada', 'casa da música', 'avenida dos aliados',
        'aliados', 'foyer', 'restaurante', 'bar dos artistas', 'cybermúsica',
    )
    if venue and any(marker in venue.casefold() for marker in home_markers):
        return venue, CITY
    return venue, ''


def parse_card(card):
    link = card.select_one('h2 a[href]')
    time_element = card.select_one('time[datetime]')
    if not link or not time_element:
        return None

    timestamp = time_element.get('datetime', '')
    match = re.match(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})', timestamp)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    title = clean_text(link)
    url = canonical_event_url(link.get('href', ''))
    venue, city = location_from_card(card, time_element)
    description_element = card.select_one('[itemprop="description"]')
    description = clean_text(description_element) or None
    if not title or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'PT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def listing_records(session):
    # The rebuilt site launched in 2023. This early lower bound keeps every
    # event still present in its public archive and also includes future pages.
    params = {'filter[after]': '2000-01-01', 'filter[reservations]': '0'}
    page = 1
    records = []
    while True:
        url = AGENDA_URL if page == 1 else f'{AGENDA_URL}page/{page}/'
        soup = get_html(session, url, params=params)
        cards = soup.select('[itemtype="https://schema.org/Event"]')
        if not cards:
            break
        for card in cards:
            record = parse_card(card)
            if record:
                records.append(record)
        if not soup.select_one('link[rel="next"], a[rel="next"]'):
            break
        page += 1
    return records


def detail_description(session, record):
    soup = get_html(session, record['url'])
    header = soup.select_one('.wp-block-allegro-post-header')
    if not header:
        return record['description']

    content_column = header.find_parent('div', class_='wp-block-column')
    if not content_column:
        return record['description']

    parts = []
    for element in content_column.select('p.wp-block-paragraph'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)

    # Programme blocks are sometimes siblings of the introductory columns.
    for heading in soup.select('h2.wp-block-heading'):
        if clean_text(heading).casefold() != 'programa':
            continue
        programme_column = heading.find_parent('div', class_='wp-block-column')
        programme = clean_text(programme_column)
        if programme and programme not in parts:
            parts.append(programme)

    detail = clean_text('\n\n'.join(parts))
    return detail or record['description']


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = listing_records(session)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, record): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class CasaDaMusicaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='casadamusica_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CasaDaMusicaComCrawler().run()


if __name__ == '__main__':
    main()
