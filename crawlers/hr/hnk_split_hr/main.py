import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hnk-split.hr/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'raspored')
SOURCE = 'Hrvatsko narodno kazalište Split'
CITY = 'Split'
IN_SCOPE_CATEGORIES = {'Opera', 'Balet', 'Koncert'}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'hr-HR,hr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
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


def occurrence_date(value, today=None):
    """Resolve the schedule's day/month against its rolling, near-term feed."""
    match = re.fullmatch(r'\s*(\d{1,2})\.(\d{1,2})\.\s*', value or '')
    if not match:
        return None
    today = today or date.today()
    try:
        candidate = date(today.year, int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None
    if candidate < today - timedelta(days=183):
        candidate = candidate.replace(year=today.year + 1)
    elif candidate > today + timedelta(days=183):
        candidate = candidate.replace(year=today.year - 1)
    return candidate.isoformat()


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\b', value or '')
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def listing_records(soup):
    records = []
    for item in soup.select('.event-schedule__item'):
        category = clean_text(item.select_one('.event-schedule__category'))
        if category not in IN_SCOPE_CATEGORIES:
            continue

        # A touring appearance cannot defensibly inherit the institution's
        # home city when the schedule supplies only a venue name.
        labels = ' '.join(clean_text(node) for node in item.select('.event-schedule__guest'))
        if 'GOSTOVANJE HNK SPLIT' in labels.upper():
            continue

        link = item.select_one('a.event-schedule__link[href]')
        title = clean_text(item.select_one('.event-schedule__headline'))
        event_date = occurrence_date(clean_text(item.select_one('.event-schedule__start-date')))
        venue = clean_text(item.select_one('.event-schedule__location'))
        url = urljoin(SCHEDULE_URL, link.get('href', '').strip()) if link else ''
        if not title or not event_date or not url or not venue or venue.casefold() == CITY.casefold():
            continue

        description_parts = [
            clean_text(item.select_one('.event-schedule__description')),
            clean_text(item.select_one('.event-opis_izvedbe')),
        ]
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(clean_text(item.select_one('.event-schedule__start-time'))),
            'venue': venue,
            'city': CITY,
            'country_code': 'HR',
            'description': '\n\n'.join(part for part in description_parts if part) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(session, record):
    soup = BeautifulSoup(get_response(session, record['url']).text, 'html.parser')
    detail = soup.select_one('article.article-detail, main.c-page-content')
    text = clean_text(detail)
    return text or record['description']


class HnkSplitHrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hnk_split_hr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        soup = BeautifulSoup(get_response(session, SCHEDULE_URL).text, 'html.parser')
        records = listing_records(soup)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(detail_description, session, record): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape HNK Split event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    HnkSplitHrCrawler().run()


if __name__ == '__main__':
    main()
