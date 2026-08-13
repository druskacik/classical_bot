import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oft.it/it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'stagione.php')
SOURCE = 'Orchestra Filarmonica di Torino'
SEASONS = (40, 45, 30)
OTHER_CONCERTS_SEASON = 9

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

DATE_RE = re.compile(
    r'(\d{1,2})/(\d{1,2})/(\d{2,4})'
    r'(?:\s*ore\s*(\d{1,2}):(\d{2}))?\s*'
    r'(.*?)(?=\d{1,2}/\d{1,2}/\d{2,4}|$)',
    re.I,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def event_cards(soup):
    cards = []
    for heading in soup.select('main h2.uk-h4'):
        card = heading.find_parent('div', class_='uk-panel-box')
        if card is not None:
            cards.append(card)
    return cards


def card_event_id(card):
    grid = card.find_parent('div', class_='uk-grid')
    if not grid:
        return None
    anchor = grid.select_one('a[name^="event_"], a[id^="event_"]')
    value = anchor.get('name') or anchor.get('id') if anchor else ''
    match = re.search(r'event_(\d+)', value or '')
    return match.group(1) if match else None


def card_header(card, heading):
    parts = []
    for sibling in heading.previous_siblings:
        text = clean_text(sibling)
        if text:
            parts.append(text)
    return clean_text(' '.join(reversed(parts))).replace('\n', ' ')


def card_description(heading):
    parts = []
    for sibling in heading.next_siblings:
        text = clean_text(sibling)
        if text and text.casefold() not in {'torna alla stagione', 'vedi programma completo'}:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_location(value):
    location = clean_text(value).replace('\n', ' ')
    location = re.sub(r'^(?:Prove di lavoro|Prove generali)\s+', '', location, flags=re.I)
    location = re.sub(r'^(?:LIETO FINE\?|OFFICINA)\s+', '', location, flags=re.I)

    city = 'Torino'
    city_match = re.search(r'\s+-\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .\'’]+)$', location)
    if city_match and not re.match(
        r'^(?:via|viale|piazza|corso|strada)\b', city_match.group(1), re.I
    ):
        city = city_match.group(1).strip()
        location = location[:city_match.start()].strip()

    # A leading separator means the page supplied a city but no venue.
    if location.startswith('-'):
        return None

    location = re.sub(
        r'\s+-\s+(?:via|viale|piazza|corso|strada)\b.*$', '', location, flags=re.I
    ).strip()
    location = re.sub(r'\s+\d+[A-Za-z]?$','', location).strip()

    # An address alone is not a venue. OFT otherwise supplies a named hall/place.
    if re.match(r'^(?:via|viale|piazza|corso|strada)\b', location, re.I):
        return None
    if not location or not city:
        return None
    return location, city


def parse_card(card, url):
    heading = card.select_one('h2.uk-h4')
    title = clean_text(heading)
    if not heading or not title or title.casefold() == 'laboratorio':
        return []

    description = card_description(heading)
    evidence = f'{title}\n{description or ""}'
    if (
        re.search(r'rinunciare al concerto dal vivo', evidence, re.I)
        and re.search(r'\b(?:streaming|youtube|canali social)\b', evidence, re.I)
    ):
        return []
    records = []
    for match in DATE_RE.finditer(card_header(card, heading)):
        year = int(match.group(3))
        year += 2000 if year < 100 else 0
        try:
            event_date = date(year, int(match.group(2)), int(match.group(1))).isoformat()
        except ValueError:
            continue

        time_from = None
        if match.group(4) and 0 <= int(match.group(4)) <= 23:
            time_from = f'{int(match.group(4)):02d}:{match.group(5)}'
        location = parse_location(match.group(6))
        if not location:
            continue
        venue, city = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


def listing_event_urls(soup, season):
    urls = []
    for link in soup.select('main a[href*="event="]'):
        url = urljoin(CALENDAR_URL, link.get('href', ''))
        if f'season={season}' in url and url not in urls:
            urls.append(url)
    return urls


def fetch_detail(session, url):
    soup = fetch_soup(session, url)
    cards = event_cards(soup)
    return parse_card(cards[0], url) if cards else []


class OftItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oft_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        detail_urls = []
        for season in SEASONS:
            url = f'{CALENDAR_URL}?season={season}'
            soup = fetch_soup(session, url)
            detail_urls.extend(listing_event_urls(soup, season))

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, session, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape OFT concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for archived in (False, True):
            suffix = '&cal_type=archive' if archived else ''
            page_url = f'{CALENDAR_URL}?season={OTHER_CONCERTS_SEASON}{suffix}'
            soup = fetch_soup(session, page_url)
            for card in event_cards(soup):
                event_id = card_event_id(card)
                url = f'{page_url}#event_{event_id}' if event_id else page_url
                records.extend(parse_card(card, url))

        return sorted(
            records,
            key=lambda row: (
                row['date'], row['time_from'] or '', row['title'], row['venue'], row['city']
            ),
        )


def main():
    OftItCrawler().run()


if __name__ == '__main__':
    main()
