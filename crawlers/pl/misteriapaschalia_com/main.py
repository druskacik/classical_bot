import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://misteriapaschalia.com/'
SOURCE = 'Misteria Paschalia'
PROGRAM_URL = urljoin(SOURCE_URL, 'program/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'stycznia': 1,
    'lutego': 2,
    'marca': 3,
    'kwietnia': 4,
    'maja': 5,
    'czerwca': 6,
    'lipca': 7,
    'sierpnia': 8,
    'września': 9,
    'października': 10,
    'listopada': 11,
    'grudnia': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})\b', value.lower())
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def city_for_venue(venue):
    normalized = venue.lower()
    if 'wielicz' in normalized:
        return 'Wieliczka'
    # The festival is based in Kraków and its programme locations are in Kraków;
    # Tyniec is an administrative district of the city.
    return 'Kraków'


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.page-header h1'))
    event_date = parse_date(clean_text(soup.select_one('.sidebar__date-date')))
    venue = clean_text(soup.select_one('.sidebar__location'))
    if not title or not event_date or not venue:
        return None

    time_text = clean_text(soup.select_one('.sidebar__date-time'))
    time_match = re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', time_text)
    description_parts = [
        clean_text(element)
        for element in soup.select('.single-event__intro, .single-event__desc')
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'PL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MisteriaPaschaliaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='misteriapaschalia_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            home_soup = BeautifulSoup(home_response.text, 'html.parser')

            feed_urls = {PROGRAM_URL}
            for link in home_soup.select('a[href*="archiwum"][href*="y="]'):
                feed_urls.add(urljoin(SOURCE_URL, link.get('href', '')))

            event_urls = set()
            for feed_url in sorted(feed_urls):
                response = session.get(feed_url, timeout=45)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                event_urls.update(
                    urljoin(SOURCE_URL, link['href'])
                    for link in soup.select('a[href*="/event/"][href]')
                )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Misteria Paschalia event feeds',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        def fetch_event(event_url):
            response = session.get(event_url, timeout=45)
            response.raise_for_status()
            return parse_event_page(response.text, event_url)

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in event_urls}
            for future in as_completed(futures):
                event_url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Misteria Paschalia event',
                        event='crawler_event_fetch_failed',
                        level='warning',
                        url=event_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MisteriaPaschaliaComCrawler().run()


if __name__ == '__main__':
    main()
