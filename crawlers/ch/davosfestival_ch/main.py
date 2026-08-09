import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://davosfestival.ch/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiv')
SOURCE = 'Davos Festival'
CITY = 'Davos'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}

# Detail URLs consistently contain the displayed date and time. This excludes
# editorial and navigation pages while retaining Festival, Singwoche and New
# Year's concert events.
EVENT_PATH = re.compile(r'/20\d{2}/.+-\d{2}-\d{2}-20\d{2}-\d{1,2}h\d{2}(?:-|$)')
DATE_TIME = re.compile(
    r'\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(20\d{2}),\s*'
    r'(\d{1,2})(?:[.:](\d{2}))?(?:\s*[–-]\s*\d{1,2}(?:[.:]\d{2})?)?\s*Uhr\b'
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def is_event_url(url):
    return urlparse(url).netloc == urlparse(SOURCE_URL).netloc and bool(
        EVENT_PATH.search(urlparse(url).path)
    )


def discover_event_urls(session):
    root = ElementTree.fromstring(get_response(session, SITEMAP_URL).content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    urls = {
        node.text.strip()
        for node in root.findall('.//sm:loc', namespace)
        if node.text and is_event_url(node.text.strip())
    }

    # The sitemap can lag behind the live programme. Follow every programme
    # and calendar linked from the archive, plus the current archive itself.
    archive_soup = BeautifulSoup(get_response(session, ARCHIVE_URL).text, 'html.parser')
    listing_urls = {ARCHIVE_URL}
    for link in archive_soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if re.search(r'/(?:programm|kalender)$', urlparse(url).path):
            listing_urls.add(url)

    for listing_url in sorted(listing_urls):
        soup = archive_soup if listing_url == ARCHIVE_URL else BeautifulSoup(
            get_response(session, listing_url).text, 'html.parser'
        )
        urls.update(
            urljoin(listing_url, link.get('href', ''))
            for link in soup.select('a[href]')
            if is_event_url(urljoin(listing_url, link.get('href', '')))
        )
    return sorted(urls)


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.find('h1')
    title = clean_text(heading)
    if not title or heading is None:
        return None

    section = heading.find_parent('section')
    event_text = clean_text(section)
    match = DATE_TIME.search(event_text)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except (TypeError, ValueError):
        return None

    # On detail pages the venue is the first non-empty line after the date/time
    # metadata (with an optional duration line in between).
    remainder = event_text[match.end():].lstrip(' \n')
    lines = [line.strip() for line in remainder.splitlines() if line.strip()]
    if lines and lines[0].lower().startswith('dauer:'):
        lines.pop(0)
    venue = lines[0] if lines else ''
    venue = re.sub(r'^Ort:\s*', '', venue, flags=re.I).strip()
    if not venue or venue.casefold() == CITY.casefold() or venue == 'Zurück zur Übersicht':
        return None

    # The main content column includes the introductory text and full musical
    # programme, but excludes the ticket-price sidebar and site footer.
    content = heading.find_parent(class_=lambda value: value and 'lg:col-span-4' in value)
    description = clean_text(content) or event_text
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{int(match.group(4)):02d}:{match.group(5) or "00"}',
        'venue': venue,
        'city': CITY,
        'country_code': 'CH',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(session, url):
    return parse_event(get_response(session, url).text, url)


class DavosFestivalChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='davosfestival_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        # The calendars also contain talks, meals and participatory events.
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
        urls = discover_event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Davos Festival event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    DavosFestivalChCrawler().run()


if __name__ == '__main__':
    main()
