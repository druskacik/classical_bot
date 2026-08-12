import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.asam.it/'
SOURCE = 'ASAM - Associazione Siracusana Amici della Musica'
DETAIL_URL = SOURCE_URL + 'concerto{number:02d}.html'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

DATE_TIME_RE = re.compile(
    r'\b(\d{1,2}/\d{1,2}/\d{4})\s+ore\s+(\d{1,2}[:.]\d{2})\b', re.I
)
KNOWN_VENUES = {
    'teatro massimo città di siracusa': 'Teatro Massimo Città di Siracusa',
    'teatro greco di siracusa': 'Teatro Greco di Siracusa',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_venue(value):
    normalized = clean_text(value)
    folded = normalized.casefold()
    for prefix, venue in KNOWN_VENUES.items():
        if folded.startswith(prefix):
            return venue, 'Siracusa'
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h2')
    headings = soup.select('h5, h6')
    date_index = next(
        (index for index, node in enumerate(headings) if DATE_TIME_RE.search(clean_text(node))),
        None,
    )
    if title_node is None or date_index is None or date_index + 1 >= len(headings):
        return None

    title = clean_text(title_node)
    match = DATE_TIME_RE.search(clean_text(headings[date_index]))
    location = parse_venue(headings[date_index + 1])
    if not title or match is None or location is None:
        return None

    try:
        event_date = datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
        time_from = datetime.strptime(match.group(2).replace('.', ':'), '%H:%M').time().strftime('%H:%M')
    except ValueError:
        return None

    description_parts = []
    for node in headings[date_index + 2:]:
        text = clean_text(node)
        if text and text.casefold() not in {'programma', 'biografia', 'biografie', 'biglietti'}:
            if not description_parts or text != description_parts[-1]:
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


class AsamItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='asam_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        records = []
        consecutive_missing = 0

        # ASAM keeps old, unlinked detail pages online. Probe the stable numeric
        # detail-page scheme so those performances remain covered as well.
        for number in range(1, 100):
            url = DETAIL_URL.format(number=number)
            try:
                response = session.get(url, timeout=45)
                if response.status_code == 404:
                    consecutive_missing += 1
                    if consecutive_missing >= 10:
                        break
                    continue
                response.raise_for_status()
                consecutive_missing = 0
                record = parse_detail(response.content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch ASAM concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AsamItCrawler().run()


if __name__ == '__main__':
    main()
