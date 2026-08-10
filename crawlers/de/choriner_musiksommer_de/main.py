import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.choriner-musiksommer.de/index.php/de/'
SOURCE = 'Choriner Musiksommer'
VENUE = 'Kloster Chorin'
CITY = 'Chorin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'märz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def programme_urls(session):
    home = get_page(session, SOURCE_URL)
    listing_urls = {
        canonical_url(urljoin(SOURCE_URL, anchor.get('href')))
        for anchor in home.select('a[href]')
        if re.search(r'/programm-\d{4}(?:$|[?#])', anchor.get('href', ''))
    }

    event_urls = set()
    for listing_url in listing_urls:
        listing = get_page(session, listing_url)
        for anchor in listing.select('a[href*="/de/programm-blog/"]'):
            event_urls.add(canonical_url(urljoin(listing_url, anchor.get('href'))))

    # The homepage also exposes the latest programme items independently of
    # the full programme page.
    for anchor in home.select('a[href*="/de/programm-blog/"]'):
        event_urls.add(canonical_url(urljoin(SOURCE_URL, anchor.get('href'))))
    return sorted(event_urls)


def parse_date_and_time(text):
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s+(\d{4})'
        r'(?:\s*[\u2022·|]\s*(\d{1,2})(?::(\d{2}))?\s*Uhr)?',
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        date = datetime(int(match.group(3)), month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5) or 0)
        if hour <= 23 and minute <= 59:
            time_from = f'{hour:02d}:{minute:02d}'
    return date, time_from


def make_record(soup, url):
    article = soup.select_one('.com-content-article.item-page')
    body = article.select_one('.com-content-article__body') if article else None
    heading = article.select_one('h1, h2') if article else None
    title = clean_text(heading)
    body_text = clean_text(body)
    date, time_from = parse_date_and_time(body_text)
    if not title or not date or not body_text:
        return None

    # Ticket and contact information follows the editorial concert text and is
    # not useful to the downstream programme extractor.
    description = re.split(
        r'\n(?:Änderungen vorbehalten|Tickets?\b|Der Vorverkauf\b)',
        body_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'DE',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in programme_urls(session):
        try:
            record = make_record(get_page(session, url), url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


class ChorinerMusiksommerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='choriner_musiksommer_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ChorinerMusiksommerDeCrawler().run()


if __name__ == '__main__':
    main()
