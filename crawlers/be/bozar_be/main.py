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


SOURCE_URL = 'https://www.bozar.be/'
CALENDAR_URL = urljoin(SOURCE_URL, 'en/calendar')
SOURCE = 'Bozar'
CONCERT_SECTION = '527'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_ranges(start, count=25):
    year, month = start.year, start.month
    for _ in range(count):
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = date(next_year, next_month, 1).fromordinal(
            date(next_year, next_month, 1).toordinal() - 1
        )
        yield date(year, month, 1).isoformat(), end.isoformat()
        year, month = next_year, next_month


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=4))
    return session


def listing_urls(session):
    urls = set()
    for start, end in month_ranges(date.today()):
        try:
            soup = get_soup(
                session,
                CALENDAR_URL,
                params={'from': start, 'to': end, 'section': CONCERT_SECTION},
            )
        except requests.RequestException as error:
            log_message(
                'Failed to scrape calendar month',
                event='crawler_page_failed',
                level='warning',
                url=f'{CALENDAR_URL}?from={start}&to={end}&section={CONCERT_SECTION}',
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('a.card-link[href*="/calendar/"]'):
            urls.add(urljoin(SOURCE_URL, link.get('href')))
    return sorted(urls)


def event_description(soup):
    section = soup.select_one('section.event-page__description')
    description = clean_text(section)
    return description or None


def event_location(soup):
    location = soup.select_one('.practical-info__content-location')
    if not location:
        return None
    venue = clean_text(location.select_one('.practical-info__hall-building'))
    address = clean_text(location.select_one('address'))
    if not venue or not address:
        return None
    match = re.search(r'\b\d{4}\s+([A-Za-zÀ-ÖØ-öø-ÿ .\'-]+)', address)
    if not match:
        return None
    city = match.group(1).strip().title()
    if not city:
        return None
    return venue, city


def parse_detail(url, soup):
    title = clean_text(soup.select_one('.event-infos__name h1'))
    location = event_location(soup)
    if not title or not location:
        return []
    venue, city = location
    description = event_description(soup)
    records = []
    for performance in soup.select('.paragraph--type--event-perf[data-start-date]'):
        raw_date = performance.get('data-start-date', '')[:10]
        try:
            event_date = date.fromisoformat(raw_date).isoformat()
        except ValueError:
            continue
        visible = clean_text(performance)
        time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', visible)
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'BE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class BozarBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bozar_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        urls = listing_urls(session)
        records = []
        # Bozar rate-limits large request bursts, so keep detail concurrency low.
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(get_soup, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(url, future.result()))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape concert detail',
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
    BozarBeCrawler().run()


if __name__ == '__main__':
    main()
