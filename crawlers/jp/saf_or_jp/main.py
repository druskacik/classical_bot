import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.saf.or.jp/'
STAGES_URL = urljoin(SOURCE_URL, 'stages/')
SOURCE = 'Saitama Arts Foundation'
CITY = 'Saitama'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.8',
}

VENUE_CITIES = {
    '彩の国さいたま芸術劇場': CITY,
    '埼玉会館': CITY,
    '熊谷文化創造館さくらめいと': 'Kumagaya',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_links(soup):
    return {
        urljoin(SOURCE_URL, anchor['href'])
        for anchor in soup.select('a[href*="/stages/detail/"]')
    }


def listing_urls(session):
    """Return current, future, and every year exposed by the archive control."""
    first_page = get_soup(session, STAGES_URL)
    urls = detail_links(first_page)

    page_url = next(
        (
            urljoin(SOURCE_URL, anchor['href'])
            for anchor in first_page.select('a[href]')
            if clean_text(anchor) == '次へ' and '/stages/page/' in anchor['href']
        ),
        None,
    )
    while page_url:
        page = get_soup(session, page_url)
        urls.update(detail_links(page))
        page_url = next(
            (
                urljoin(SOURCE_URL, anchor['href'])
                for anchor in page.select('a[href]')
                if clean_text(anchor) == '次へ' and '/stages/page/' in anchor['href']
            ),
            None,
        )

    years = {
        option.get('value')
        for option in first_page.select('#year option[value]')
        if re.fullmatch(r'\d{4}', option.get('value', ''))
    }
    # The site's year-only query is broken for some years (for example 2025),
    # while the documented year+month query reliably returns the archive.
    archive_queries = [
        {'years': year, 'month': str(month)}
        for year in sorted(years)
        for month in range(1, 13)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, STAGES_URL, params=params): params
            for params in archive_queries
        }
        for future in as_completed(futures):
            params = futures[future]
            try:
                urls.update(detail_links(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape archive month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{STAGES_URL}?years={params["years"]}&month={params["month"]}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def table_value(soup, heading):
    for row in soup.select('table tr'):
        label = row.find('th')
        value = row.find('td')
        if label and value and heading in clean_text(label):
            return clean_text(value)
    return ''


def resolve_city(venue):
    for venue_name, city in VENUE_CITIES.items():
        if venue_name in venue:
            return city
    # Japanese venue/address text frequently states the municipality directly.
    city_match = re.search(r'([一-鿿]{2,8}市)', venue)
    return city_match.group(1) if city_match else None


def occurrences(value):
    date_pattern = re.compile(
        r'(?:(20\d{2})\s*年\s*)?'
        r'(?:(\d{1,2})\s*月\s*)?'
        r'(\d{1,2})\s*日'
    )
    matches = list(date_pattern.finditer(value))
    results = []
    current_year = None
    current_month = None
    for index, match in enumerate(matches):
        current_year = match.group(1) or current_year
        current_month = match.group(2) or current_month
        if not current_year or not current_month:
            continue
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        time_matches = re.findall(
            r'(\d{1,2})\s*[:：]\s*(\d{2})',
            value[match.end():segment_end],
        )
        for hour, minute in time_matches:
            try:
                event_date = date(
                    int(current_year), int(current_month), int(match.group(3))
                ).isoformat()
                event_time = f'{int(hour):02d}:{int(minute):02d}'
            except ValueError:
                continue
            pair = (event_date, event_time)
            if pair not in results:
                results.append(pair)
    return results


def parse_detail(url, soup):
    title_node = soup.select_one('#common_main_contents h1.h1_ttl')
    title = clean_text(title_node)
    date_text = table_value(soup, '日時')
    venue = table_value(soup, '会場')

    if not date_text:
        summary = soup.select_one('.information_main_right')
        date_text = clean_text(summary)
    if not venue:
        summary = soup.select_one('.information_main_right')
        summary_text = clean_text(summary)
        venue = next(
            (name for name in VENUE_CITIES if name in summary_text),
            '',
        )

    city = resolve_city(venue)
    dates = occurrences(date_text)
    if not title or not venue or not city or not dates:
        return []

    body = soup.select_one('.contents_area_01')
    description = clean_text(body) or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, event_time in dates
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_detail(url, future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class SafOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='saf_or_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SafOrJpCrawler().run()


if __name__ == '__main__':
    main()
