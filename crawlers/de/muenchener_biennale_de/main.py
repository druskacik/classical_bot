import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.muenchener-biennale.de/de'
CALENDAR_URL = f'{SOURCE_URL}/programm/kalender'
SOURCE = 'Münchener Biennale'
CITY = 'München'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    try:
        day, month, year = (int(part) for part in value.split('-'))
        return date(year, month, day).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def parse_calendar(soup):
    records = []
    for item in soup.select('.events-t1 .list > .element[data-date]'):
        title = clean_text(item.select_one('.title'))
        event_date = parse_date(item.get('data-date'))
        link = item.select_one('.title-tags > a[href]')
        location = item.select_one('.location')
        venue = clean_text(location.select_one('.hover-text') if location else None)
        url = urljoin(CALENDAR_URL, link.get('href')) if link else ''
        time_text = clean_text(item.select_one('.time-from'))
        time_match = re.fullmatch(r'(\d{1,2}):(\d{2})', time_text)
        time_from = None
        if time_match and int(time_match.group(1)) < 24 and int(time_match.group(2)) < 60:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

        # The published festival calendar is local to Munich. Items without a
        # venue are omitted rather than assigning the city as a fake venue.
        if not title or not event_date or not url or not venue:
            log_message(
                'Skipped Münchener Biennale calendar item without required data',
                event='crawler_item_skipped',
                level='warning',
                url=url or CALENDAR_URL,
                error_type='IncompleteEventData',
                error_message='Missing title, date, URL, or venue',
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'DE',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(url):
    soup = get_soup(make_session(), url)
    detail = soup.select_one('.event-t1')
    return clean_text(detail) or None


class MuenchenerBiennaleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='muenchener_biennale_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        records = parse_calendar(get_soup(session, CALENDAR_URL))
        records_by_url = {}
        for record in records:
            records_by_url.setdefault(record['url'], []).append(record)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(detail_description, url): url
                for url in records_by_url
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    description = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Münchener Biennale event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                for record in records_by_url[url]:
                    record['description'] = description

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['venue'], item['title']
            ),
        )


def main():
    MuenchenerBiennaleDeCrawler().run()


if __name__ == '__main__':
    main()
