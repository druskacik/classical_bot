import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://salaceciliameireles.rj.gov.br/'
PROGRAM_URL = f'{SOURCE_URL}programacao/'
SOURCE = 'Sala Cecília Meireles'
CITY = 'Rio de Janeiro'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_occurrences(card, year):
    occurrences = []
    for node in card.select('.date.full'):
        text = clean_text(node)
        match = re.search(
            r'\b(\d{1,2})\s+([a-zç]{3})\b.*?\b(\d{1,2})\s*[hH](\d{2})?',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        month = MONTHS.get(match.group(2).lower())
        if not month:
            continue
        try:
            event_date = date(year, month, int(match.group(1))).isoformat()
        except ValueError:
            continue
        occurrences.append((event_date, f'{int(match.group(3)):02d}:{match.group(4) or "00"}'))
    return occurrences


def listing_items(soup, year):
    items = []
    for card in soup.select('.eventos .event'):
        title = clean_text(card.select_one('.title'))
        url = card.get('href') or ''
        if not title or not url:
            continue
        for event_date, time_from in parse_occurrences(card, year):
            items.append({
                'title': title,
                'date': event_date,
                'time_from': time_from,
                'url': url,
            })
    return items


def scrape_archive(session, archive):
    soup = get_soup(session, PROGRAM_URL, params={'me': archive})
    return listing_items(soup, int(archive[:4]))


def detail_data(session, url):
    soup = get_soup(session, url)
    venue = clean_text(soup.select_one('.event.info .venue'))

    description_node = soup.select_one('.main-area.wrapper')
    description = None
    if description_node:
        # The first direct child is a ticket panel and related-event blocks are
        # navigation, not programme notes. The remaining body preserves the
        # synopsis, performers, composers, and works.
        direct_children = description_node.find_all(recursive=False)
        body_children = direct_children[1:] if len(direct_children) > 1 else direct_children
        for child in body_children:
            for unwanted in child.select('.proximos-eventos, .related, .link, .price'):
                unwanted.decompose()
        description = clean_text('\n\n'.join(clean_text(child) for child in body_children)) or None
    return venue, description


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    current_soup = get_soup(session, PROGRAM_URL)

    archives = sorted({
        node.get('me') for node in current_soup.select('.filter-link[me]')
        if re.fullmatch(r'\d{6}', node.get('me') or '')
    })
    items = listing_items(current_soup, date.today().year)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scrape_archive, session, archive): archive for archive in archives}
        for future in as_completed(futures):
            archive = futures[future]
            try:
                items.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape programme archive',
                    event='crawler_archive_failed',
                    level='warning',
                    url=f'{PROGRAM_URL}?me={archive}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique_items = {
        (item['url'], item['date'], item['time_from']): item
        for item in items
    }
    details = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(detail_data, session, url): url
            for url in {item['url'] for item in unique_items.values()}
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for item in unique_items.values():
        venue, description = details.get(item['url'], ('', None))
        if not venue:
            continue
        records.append({
            **item,
            'venue': venue,
            'city': CITY,
            'country_code': 'BR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class SalaCeciliaMeirelesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='salaceciliameireles_rj_gov_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
        upload_target='potential',
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

    def scrape(self):
        return get_concerts()


def main():
    SalaCeciliaMeirelesCrawler().run()


if __name__ == '__main__':
    main()
