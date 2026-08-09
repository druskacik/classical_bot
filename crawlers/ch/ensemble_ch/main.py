import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ensemble.ch/'
SOURCE = 'Ensemble für neue Musik Zürich'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
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

DATE_RE = re.compile(
    r'\b(\d{1,2})\.\s*'
    r'(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)'
    r'\s+(20\d{2})\b',
    re.IGNORECASE,
)
PERFORMANCE_RE = re.compile(
    r'\b((?:[01]?\d|2[0-3]):[0-5]\d)\s+([^,\n]+),\s*([^\n]+)'
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(text):
    dates = []
    for day, month_name, year in DATE_RE.findall(text):
        try:
            dates.append(
                date(int(year), MONTHS[month_name.lower()], int(day)).isoformat()
            )
        except ValueError:
            continue
    return dates


def parse_performances(text):
    performances = []
    for time_from, city, venue in PERFORMANCE_RE.findall(text):
        city = city.strip(' ,')
        venue = venue.strip(' ,')
        if city and venue:
            performances.append((time_from, city, venue))
    return performances


def parse_event_page(page):
    body = page.select_one('bodycopy')
    columns = body.select('column-set:first-of-type > column-unit') if body else []
    if len(columns) < 3:
        return []

    dates = parse_dates(clean_text(columns[0]))
    performances = parse_performances(clean_text(columns[2]))
    title_element = columns[1].find(['b', 'strong'])
    title = clean_text(title_element)
    description = clean_text(columns[1]) or None
    page_slug = page.get('page-url', '').strip()

    if not dates or not performances or not title or not page_slug:
        return []

    if len(dates) == 1:
        dates = dates * len(performances)
    if len(dates) != len(performances):
        return []

    url = urljoin(SOURCE_URL, page_slug)
    records = []
    for event_date, (time_from, city, venue) in zip(dates, performances):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'CH',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class EnsembleChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemble_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Ensemble für neue Musik Zürich programme',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for page in soup.select('.pages .page[page-url]'):
            records.extend(parse_event_page(page))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    EnsembleChCrawler().run()


if __name__ == '__main__':
    main()
