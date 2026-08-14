import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.radiofilharmonischorkest.nl/'
CONCERTS_URL = f'{SOURCE_URL}concerten/'
SOURCE = 'Radio Filharmonisch Orkest'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1,
    'februari': 2,
    'maart': 3,
    'april': 4,
    'mei': 5,
    'juni': 6,
    'juli': 7,
    'augustus': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_dutch_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})\b',
        clean_text(value).lower(),
    )
    if not match:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def listing_urls(session):
    soup = get_soup(session, CONCERTS_URL)
    urls = [
        link.get('href')
        for link in soup.select('.facetwp-template .post--search-event a[href]')
        if link.get('href', '').startswith(CONCERTS_URL)
    ]
    page = 2
    total_pages = None
    while total_pages is None or page <= total_pages:
        facet_data = {
            'facets': {
                'search': '',
                'calendar': [],
                'series': [],
                'ensemble': [],
                'venues': [],
                'pagination': [],
            },
            'frozen_facets': {},
            'http_params': {
                'get': {'_paged': str(page)},
                'uri': 'concerten',
                'url_vars': [],
            },
            'template': 'wp',
            'extras': {'sort': 'default'},
            'soft_refresh': 1,
            'is_bfcache': 1,
            'first_load': 0,
            'paged': str(page),
        }
        response = session.post(
            CONCERTS_URL,
            params={'_paged': page},
            json={'action': 'facetwp_refresh', 'data': facet_data},
            headers={
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Origin': SOURCE_URL.rstrip('/'),
                'Referer': CONCERTS_URL,
                'X-Requested-With': 'XMLHttpRequest',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        soup = BeautifulSoup(payload.get('template') or '', 'html.parser')
        page_urls = [
            link.get('href')
            for link in soup.select('.facetwp-template .post--search-event a[href]')
            if link.get('href', '').startswith(CONCERTS_URL)
        ]
        page_urls = list(dict.fromkeys(page_urls))
        if not page_urls:
            break
        urls.extend(page_urls)
        total_pages = (payload.get('settings') or {}).get('pager', {}).get('total_pages')
        if total_pages is not None:
            total_pages = int(total_pages)
        page += 1
    return list(dict.fromkeys(urls))


def meta_value(soup, label):
    for row in soup.select('.events-single__meta-row'):
        row_label = clean_text(row.select_one('.label')).rstrip(':').lower()
        if row_label == label.lower():
            return clean_text(row.select_one('.value'))
    return ''


def resolve_location(value):
    lines = [line.strip() for line in clean_text(value).splitlines() if line.strip()]
    if not lines:
        return None, None, None
    venue = lines[0]
    city = None
    if len(lines) > 1:
        city = re.split(r'\s+[–-]\s+', lines[1], maxsplit=1)[0].strip()

    # The two principal halls are sometimes rendered without a separate city.
    if not city and 'concertgebouw' in venue.lower():
        city = 'Amsterdam'
    elif not city and 'tivolivredenburg' in venue.lower():
        city = 'Utrecht'
    country_code = 'DE' if city == 'Dortmund' else 'NL'
    return venue or None, city or None, country_code


def detail_description(soup):
    content = soup.select_one('.events-single__content')
    if not content:
        return None
    copy = BeautifulSoup(str(content), 'html.parser')
    heading = copy.select_one('h1')
    if heading:
        heading.decompose()
    text = clean_text(copy)
    return text or None


def parse_detail(soup, url):
    content = soup.select_one('.events-single__content')
    title = clean_text(content.select_one('h1')) if content else ''
    event_date = parse_dutch_date(meta_value(soup, 'Datum'))
    time_text = meta_value(soup, 'Tijd')
    time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', time_text)
    venue, city, country_code = resolve_location(meta_value(soup, 'Locatie'))
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_detail(future.result(), url)
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
            else:
                log_message(
                    'Skipped concert with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class RadiofilharmonischorkestNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='radiofilharmonischorkest_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
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
    RadiofilharmonischorkestNlCrawler().run()


if __name__ == '__main__':
    main()
