import hashlib
import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hmtm-hannover.de/'
SOURCE = 'HMTM Hannover'
CALENDAR_URL = urljoin(SOURCE_URL, 'de/alle-veranstaltungen/?no_cache=1')
ARCHIVE_URL = urljoin(
    SOURCE_URL,
    'de/alle-veranstaltungen/aktuelle-veranstaltungen/archiv/',
)
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}

# The server currently omits an intermediate certificate. Browsers recover the
# chain, but Requests cannot; keep scraping available until the site fixes it.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u00ad', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.verify = False
    session.mount('https://', HTTPAdapter(
        pool_connections=16,
        pool_maxsize=16,
        max_retries=Retry(
            total=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def add_month(year, month, offset):
    ordinal = year * 12 + month - 1 + offset
    return ordinal // 12, ordinal % 12 + 1


def month_url(year, month):
    # A timestamp anywhere within the desired month is accepted by pxc_calendar.
    stamp = int(datetime(year, month, 15, 12, tzinfo=timezone.utc).timestamp())
    return urljoin(
        SOURCE_URL,
        f'de/alle-veranstaltungen/calendar/{stamp}/'
        '?no_cache=1&tx_pxccalendar_eventlist%5Bcontroller%5D=Event',
    )


def archive_start_year(soup):
    years = []
    for link in soup.select('a[href*="/archiv/sommersemester-"], '
                            'a[href*="/archiv/wintersemester-"]'):
        match = re.search(r'(?:sommersemester|wintersemester)-(20\d{2})', link.get('href', ''))
        if match:
            years.append(int(match.group(1)))
    return min(years) if years else date.today().year


def parse_month_year(soup):
    heading = soup.select_one('.pxcCalendarListHeadline .kalenderheadline span')
    match = re.search(r'(20\d{2})', clean_text(heading))
    return int(match.group(1)) if match else None


def split_venue(value):
    # Ticket information follows a pipe and is not part of the venue.
    return clean_text(value).split('|', 1)[0].strip(' ,\n')


def city_from_venue(venue):
    postal = re.search(r'\b\d{5}\s+([^,;|\n]+)', venue)
    if postal:
        return postal.group(1).strip()

    folded = venue.casefold()
    known_cities = (
        'Hannover', 'Neustadt am Rübenberge', 'Braunschweig', 'Hildesheim',
        'Celle', 'Hameln', 'Langenhagen', 'Laatzen', 'Garbsen', 'Lehrte',
        'Burgdorf', 'Göttingen', 'Hamburg', 'Bremen', 'Berlin', 'Leipzig',
        'Dresden', 'Köln', 'München', 'Frankfurt am Main', 'Osnabrück',
    )
    for city in known_cities:
        if city.casefold() in folded:
            return city
    # The calendar is institution-local; entries outside Hannover normally give
    # a postal city or name the touring city explicitly.
    return 'Hannover'


def parse_card(card, year, page_url):
    date_text = clean_text(card.select_one('.subheadline'))
    day_match = re.match(r'(\d{1,2})\.', date_text)
    anchor = card.select_one('.subheadline a[id]')
    anchor_match = re.fullmatch(r'cal(\d{2})(\d{2})', anchor.get('id', '') if anchor else '')
    if not day_match or not anchor_match:
        return None

    day = int(day_match.group(1))
    month = int(anchor_match.group(2))
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*Uhr\b', date_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    title = clean_text(card.select_one('h3')).replace('\n', ' ')
    venue_node = card.find('p', recursive=False)
    venue = split_venue(venue_node)
    if not title or not venue:
        return None

    descriptions = []
    heading = card.select_one('h3')
    if heading:
        for node in heading.find_all_next('p'):
            if node.find_parent('div', class_='content-teaser') is not card:
                break
            value = clean_text(node)
            if value and value != clean_text(venue_node):
                descriptions.append(value)
    description = '\n\n'.join(dict.fromkeys(descriptions)) or None
    identity = hashlib.sha1(
        f'{event_date}|{time_from}|{venue}|{title}'.encode('utf-8')
    ).hexdigest()[:12]
    return {
        'title': title,
        'date': event_date,
        'url': f'{page_url}#event-{identity}',
        'time_from': time_from,
        'venue': venue,
        'city': city_from_venue(venue),
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_calendar(soup, page_url):
    year = parse_month_year(soup)
    if not year:
        return []
    return [
        record for card in soup.select('.pxcCalendarEventlist .content-teaser')
        if (record := parse_card(card, year, page_url))
    ]


def get_concerts():
    session = make_session()
    try:
        archive_soup = get_soup(session, ARCHIVE_URL)
        first_year = archive_start_year(archive_soup)
    except requests.RequestException as error:
        first_year = date.today().year
        log_message(
            'Failed to discover HMTM Hannover archive range',
            event='crawler_page_failed', level='warning', url=ARCHIVE_URL,
            error_type=type(error).__name__, error_message=str(error),
        )

    today = date.today()
    start_ordinal = first_year * 12
    end_ordinal = (today.year + 1) * 12 + today.month - 1
    pages = [
        month_url(ordinal // 12, ordinal % 12 + 1)
        for ordinal in range(start_ordinal, end_ordinal + 1)
    ]

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in pages}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_calendar(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape HMTM Hannover calendar month',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

    unique = {
        (item['date'], item['time_from'], item['venue'], item['title']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'],
    ))


class HmtmHannoverDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hmtm_hannover_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HmtmHannoverDeCrawler().run()


if __name__ == '__main__':
    main()
