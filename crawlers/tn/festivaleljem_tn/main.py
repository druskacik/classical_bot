import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivaleljem.tn/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programme')
SOURCE = 'Festival International de Musique Symphonique d’El Jem'
VENUE = "Amphithéâtre d'El Jem"
CITY = 'El Jem'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
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
    match = re.search(
        r'\b(\d{1,2})\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(20\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', value)
    return match.group(0).zfill(5) if match else None


def event_links(programme_soup):
    return sorted({
        urljoin(PROGRAMME_URL, link['href'])
        for link in programme_soup.select('a[href*="/events/"][href]')
    })


def parse_event(soup, url):
    title = clean_text(soup.select_one('h1'))
    page_text = clean_text(soup)
    event_date = parse_date(page_text)
    time_from = parse_time(page_text)
    description = clean_text(soup.select_one('div.prose')) or None

    if not title or not event_date:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'TN',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FestivalEljemTnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivaleljem_tn',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='TN',
        upload_target='classical',
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
            response = session.get(PROGRAMME_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Festival El Jem programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAMME_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        links = event_links(BeautifulSoup(response.text, 'html.parser'))
        for url in links:
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                record = parse_event(BeautifulSoup(detail.text, 'html.parser'), url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Festival El Jem event with incomplete required fields',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival El Jem event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    FestivalEljemTnCrawler().run()


if __name__ == '__main__':
    main()
