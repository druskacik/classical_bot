import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.divertimentoensemble.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario')
SOURCE = 'Divertimento Ensemble'

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

# The site omits the city on a number of Milan venue names.  These are all
# first-party calendar locations used by the organisation for its Milan events.
MILAN_VENUE_MARKERS = (
    'fabbrica del vapore', 'sala donatoni', 'conservatorio', 'sala verdi',
    'mamu', 'magazzino musica', 'chiesa rossa', 'gratosoglio', 'ticinello',
    "fabbrica dell'esperienza", 'bristol',
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def event_urls():
    # The date control normally prevents choosing the past in the browser, but
    # its stable query accepts an earlier date and returns the retained archive
    # together with current/future events.  categories='' is the unfiltered feed.
    response = requests.get(
        CALENDAR_URL,
        params={'date': '1970-01-01', 'categories': ''},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    paths = re.findall(r'\\"href\\":\\"(/eventi/[^\\"]+)', response.text)
    return list(dict.fromkeys(urljoin(SOURCE_URL, path) for path in paths))


def metadata_value(container, label):
    for node in container.select('span.font-light.block'):
        if clean_text(node).casefold() == label.casefold():
            values = [clean_text(span) for span in node.parent.select('span.title-5.block')]
            return [value for value in values if value]
    return []


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', value.strip())
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def parse_location(value):
    location = clean_text(value)
    if not location:
        return None
    folded = location.casefold()
    if any(marker in folded for marker in MILAN_VENUE_MARKERS):
        venue = re.sub(r'^milano\s*,\s*', '', location, flags=re.I)
        return venue, 'Milano'
    if ',' in location:
        city, venue = [part.strip() for part in location.split(',', 1)]
        if city and venue:
            return venue, city
    return None


def parse_detail(soup, url):
    container = soup.select_one('.container.events-page')
    title_node = container.select_one('h1') if container else None
    if not container or not title_node:
        return None

    title = clean_text(title_node)
    location_values = metadata_value(container, 'Location')
    date_values = metadata_value(container, 'Data & orario')
    if not title or not location_values or not date_values:
        return None

    event_date = parse_date(date_values[0])
    location = parse_location(location_values[0])
    if not event_date or not location:
        return None

    time_from = None
    for value in date_values[1:]:
        match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
        if match:
            time_from = f'{int(match.group(1)):02d}:{match.group(2)}'
            break

    description_parts = []
    grids = container.select(':scope > div.grid')
    if grids:
        for block in grids[0].select('.block-text'):
            classes = set(block.get('class', []))
            if 'md:hidden' in classes:
                continue
            text = clean_text(block)
            if text and text not in description_parts:
                description_parts.append(text)

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url):
    try:
        return parse_detail(get_soup(url), url)
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Divertimento Ensemble event',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class DivertimentoensembleItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='divertimentoensemble_it',
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
        try:
            urls = event_urls()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Divertimento Ensemble calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except Exception as error:
                    log_message(
                        'Failed to parse Divertimento Ensemble event',
                        event='crawler_item_failed',
                        level='warning',
                        url=futures[future],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    DivertimentoensembleItCrawler().run()


if __name__ == '__main__':
    main()
