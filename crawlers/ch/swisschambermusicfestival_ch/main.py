import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://swisschambermusicfestival.ch/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'Festival/Festivalprogramm')
SOURCE = 'Swiss Chamber Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = re.search(
        r'\b(\d{2}\.\d{2}\.\d{4})\s+([01]?\d|2[0-3]):([0-5]\d)',
        value,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None
    return event_date, f'{int(match.group(2)):02d}:{match.group(3)}'


def parse_location(soup):
    address = soup.select_one('.calendar-adress-place address')
    lines = list(address.stripped_strings) if address else []
    if not lines:
        return None

    venue = clean_text(lines[0])
    city = ''
    for line in reversed(lines):
        match = re.search(r'\b\d{4}\s+(.+)$', clean_text(line))
        if match:
            city = match.group(1).strip(' ,')
            break

    # Some all-day activities identify only the municipality as their place.
    # A city is not a defensible venue, so those records are omitted.
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city


def event_description(soup):
    parts = []
    for element in soup.select('article.content-tpl'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    info_table = soup.select_one('table.export-icon')
    event_date, time_from = parse_date_time(clean_text(info_table))
    location = parse_location(soup)
    if not title or not event_date or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CH',
        'description': event_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return parse_event(response.text, url)


class SwissChamberMusicFestivalChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='swisschambermusicfestival_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
        try:
            response = session.get(PROGRAM_URL, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Swiss Chamber Music Festival programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        urls = list(dict.fromkeys(
            urljoin(PROGRAM_URL, link['href'])
            for link in soup.select('a[href*="Festivalprogramm/Detail"][href]')
        ))
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to process Swiss Chamber Music Festival event',
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
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    SwissChamberMusicFestivalChCrawler().run()


if __name__ == '__main__':
    main()
