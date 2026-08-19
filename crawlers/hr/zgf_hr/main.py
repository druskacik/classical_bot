import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.zgf.hr/'
CALENDAR_URL = urljoin(SOURCE_URL, 'hr/koncertni-kalendar/')
SOURCE = 'Zagrebačka filharmonija'

# The calendar accepts arbitrary ISO date ranges and returns the complete result
# without pagination. Its archive currently begins in 2020.
CALENDAR_PARAMS = {'date-from': '2000-01-01', 'date-to': '2100-12-31'}
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
    ),
    'Accept-Language': 'hr-HR,hr;q=0.9,en;q=0.6',
}

# The orchestra's calendar uses several abbreviations and capitalizations for
# the same Zagreb venues. Touring rows are handled separately and never receive
# the home-city default.
ZAGREB_VENUE_MARKERS = (
    'lisinski',
    'kdvl',
    'hrvatsko narodno kazalište',
    'hnk',
    'tvornica kulture',
    'lauba',
    'dom hrvatske vojske zvonimir',
    'gastro globus',
    'zagrebačkog velesajma',
    'muzička akademija',
    'blagoje bersa',
    'crkva sv. križa',
    'crkvi sv. križa',
    'hrvatske matice iseljenika',
)
INVALID_VENUES = {'naknadno', 'hrt 2', 'hrt3, 20.30 sati'}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def clean_text(value):
    if value is None:
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').split()).strip()


def calendar_links():
    response = make_session().get(
        CALENDAR_URL, params=CALENDAR_PARAMS, timeout=90
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return sorted(
        {
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select(
                '.eventsListItem a.eventsListTitleCntHref[href]'
            )
        }
    )


def music_event_data(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get('@graph'), list):
            candidates = data['@graph']
        elif isinstance(data, list):
            candidates = data
        else:
            candidates = [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'MusicEvent':
                return candidate
    return None


def resolve_city(title, venue):
    venue_lower = venue.casefold()
    title_lower = title.casefold()

    # These are explicit touring rows whose publisher supplied only the city as
    # the Place name. A city is not a valid venue, so they must be skipped.
    if venue_lower in {'vukovar', 'đakovo'}:
        return None
    if venue_lower in INVALID_VENUES:
        return None
    if any(marker in venue_lower for marker in ZAGREB_VENUE_MARKERS):
        return 'Zagreb'

    # The calendar is the Zagreb Philharmonic's own performance calendar. A
    # non-tour row with a concrete venue can defensibly use its home city.
    if 'gostovanje' not in title_lower and venue:
        return 'Zagreb'
    return None


def parse_detail(url):
    response = make_session().get(url, timeout=60)
    response.raise_for_status()
    data = music_event_data(BeautifulSoup(response.text, 'html.parser'))
    if not data:
        return None

    title = clean_text(data.get('name'))
    venue = clean_text((data.get('location') or {}).get('name'))
    start = clean_text(data.get('startDate'))

    # The site publishes a few season-subscription sales placeholders as dated
    # MusicEvents. They are not concrete performances.
    if not title or 'pretplata' in title.casefold() or not venue or not start:
        return None

    try:
        start_dt = datetime.fromisoformat(start)
    except ValueError:
        return None

    city = resolve_city(title, venue)
    if not city:
        return None

    description = clean_text(
        data.get('description') or data.get('workPerformed')
    ) or None
    canonical_url = urljoin(SOURCE_URL, clean_text(data.get('url')) or url)
    return {
        'title': title,
        'date': start_dt.date().isoformat(),
        'url': canonical_url,
        'time_from': start_dt.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'HR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    links = calendar_links()
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_detail, url): url for url in links}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
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


class ZgfHrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='zgf_hr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HR',
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
        return get_concerts()


def main():
    return ZgfHrCrawler().run()


if __name__ == '__main__':
    main()
