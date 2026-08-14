import re
import time
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://marvaomusic.com/'
PROGRAMME_URL = urljoin(
    SOURCE_URL,
    'programa-12-festival-internacional-musica-marvao-2026/',
)
SOURCE = 'Festival Internacional de Música de Marvão'
HEADERS = {
    # The server rejects requests without Chromium client headers.
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'Upgrade-Insecure-Requests': '1',
}
MONTHS = {
    'JAN': 1, 'FEV': 2, 'MAR': 3, 'ABR': 4, 'MAI': 5, 'JUN': 6,
    'JUL': 7, 'AGO': 8, 'SET': 9, 'OUT': 10, 'NOV': 11, 'DEZ': 12,
}
DATE_PATTERN = re.compile(r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\b', re.I)
YEAR_PATTERN = re.compile(r'\b(20\d{2})\b')
TIME_PATTERN = re.compile(r'\b([01]?\d|2[0-3]):[0-5]\d\b')


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_page_date(soup):
    title = clean_text(soup.title)
    date_match = DATE_PATTERN.search(title)
    year_match = YEAR_PATTERN.search(title)
    if not date_match or not year_match:
        return None
    try:
        return date(
            int(year_match.group(1)),
            MONTHS[date_match.group(2).upper()],
            int(date_match.group(1)),
        ).isoformat()
    except ValueError:
        return None


def parse_day(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    event_date = parse_page_date(soup)
    if not event_date:
        return []

    records = []
    for heading in soup.select('a.accordion-toggle[href^="#"]'):
        parts = list(heading.stripped_strings)
        time_index = next(
            (index for index, part in enumerate(parts) if TIME_PATTERN.search(part)),
            None,
        )
        if time_index is None or time_index == 0:
            continue

        venue = ' '.join(parts[:time_index]).strip(' ,-')
        time_match = TIME_PATTERN.search(parts[time_index])
        title = ' '.join(parts[time_index + 1:]).strip(' -–—')
        panel_id = heading.get('href', '')
        panel = soup.select_one(panel_id) if re.fullmatch(r'#[A-Za-z0-9_-]+', panel_id) else None
        description = clean_text(panel) or None
        if not title or not venue or not time_match:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': f'{page_url}{panel_id}',
            'time_from': time_match.group(0),
            'venue': venue,
            # Every programme venue is within Marvão; headings explicitly name
            # Marvão or a landmark in the festival's compact home municipality.
            'city': 'Marvão',
            'description': description,
        })
    return records


class MarvaoMusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marvaomusic_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    @staticmethod
    def _get(session, url):
        for attempt in range(4):
            response = session.get(url, timeout=45)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            time.sleep(2 ** attempt)
        response.raise_for_status()

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            programme = self._get(session, PROGRAMME_URL)
            soup = BeautifulSoup(programme.text, 'html.parser')
            day_urls = list(dict.fromkeys(
                urljoin(SOURCE_URL, anchor['href'])
                for anchor in soup.select('a[href*="programa="]')
            ))
            if not day_urls:
                raise ValueError('No programme day pages found')

            records = []
            for index, day_url in enumerate(day_urls):
                if index:
                    time.sleep(1)
                response = self._get(session, day_url)
                records.extend(parse_day(response.text, day_url))
            if not records:
                raise ValueError('No parseable scheduled programme items found')
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape FIMM programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAMME_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'], record['title'], record['venue']
            ),
        )


def main():
    MarvaoMusicComCrawler().run()


if __name__ == '__main__':
    main()
