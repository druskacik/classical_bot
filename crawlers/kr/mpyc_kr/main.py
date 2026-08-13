import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mpyc.kr/'
LIST_URL = urljoin(SOURCE_URL, 'kr/sub/concert/guide.php')
SOURCE = 'Music in PyeongChang'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.7',
}

# The calendar tours throughout Gangwon and occasionally visits Seoul. Prefer
# explicit place names in venues, with narrowly scoped defaults for halls whose
# locations are stable and unambiguous.
CITY_MARKERS = {
    '서울': 'Seoul',
    '춘천': 'Chuncheon',
    '평창': 'Pyeongchang',
    '강릉': 'Gangneung',
    '원주': 'Wonju',
    '동해': 'Donghae',
    '태백': 'Taebaek',
    '속초': 'Sokcho',
    '삼척': 'Samcheok',
    '홍천': 'Hongcheon',
    '횡성': 'Hoengseong',
    '영월': 'Yeongwol',
    '정선': 'Jeongseon',
    '철원': 'Cheorwon',
    '화천': 'Hwacheon',
    '양구': 'Yanggu',
    '인제': 'Inje',
    '고성': 'Goseong',
    '양양': 'Yangyang',
}
VENUE_CITIES = {
    '알펜시아 콘서트홀': 'Pyeongchang',
    '알펜시아 뮤직텐트': 'Pyeongchang',
    '알펜시아 컨벤션센터': 'Pyeongchang',
    '대관령야외공연장': 'Pyeongchang',
    '대관령 트레이닝센터': 'Pyeongchang',
    '페리지홀': 'Seoul',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(params):
    response = requests.get(LIST_URL, params=params, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.url, BeautifulSoup(response.text, 'html.parser')


def available_years(soup):
    years = []
    for option in soup.select('select[name="s_year"] option'):
        value = option.get('value', '')
        if re.fullmatch(r'20\d{2}', value):
            years.append(value)
    return sorted(set(years))


def page_numbers(soup):
    pages = {1}
    for link in soup.select('a[href*="page="]'):
        values = parse_qs(urlparse(link.get('href', '')).query).get('page', [])
        if values and values[0].isdigit():
            pages.add(int(values[0]))
    return pages


def resolve_city(venue):
    for marker, city in CITY_MARKERS.items():
        if marker in venue:
            return city
    for marker, city in VENUE_CITIES.items():
        if marker in venue:
            return city
    return None


def parse_datetime(value):
    match = re.search(
        r'(20\d{2}-\d{2}-\d{2})\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
        hour = int(match.group(2)) % 12
        if match.group(4).upper() == 'PM':
            hour += 12
        return event_date, f'{hour:02d}:{int(match.group(3) or 0):02d}'
    except ValueError:
        return None, None


def labelled_values(card):
    values = {}
    for row in card.select('dl'):
        label = clean_text(row.select_one('dt'))
        value = clean_text(row.select_one('dd'))
        if label and value:
            values[label] = value
    return values


def parse_card(card):
    title = clean_text(card.select_one('h4'))
    detail_link = card.select_one('a[href*="/concert/view.php"]')
    url = urljoin(LIST_URL, detail_link.get('href', '')) if detail_link else ''
    values = labelled_values(card)
    event_date, time_from = parse_datetime(values.get('일시', ''))
    venue = clean_text(values.get('장소'))
    city = resolve_city(venue)
    if not title or not url or not event_date or not venue or not city:
        return None

    subtitle = clean_text(card.select_one('.text p'))
    description_parts = []
    if subtitle:
        description_parts.append(subtitle)
    for label in ('연주자', '프로그램'):
        value = values.get(label)
        if value:
            description_parts.append(f'{label}\n{value}')

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'KR',
        'description': clean_text('\n\n'.join(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    _, initial = get_page({'s_sche': '1', 's_year': str(date.today().year), 'page': 1})
    years = available_years(initial)
    if not years:
        years = [str(date.today().year)]

    first_pages = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(get_page, {'s_sche': state, 's_year': year, 'page': 1}): (state, year)
            for year in years
            for state in ('0', '1')
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                first_pages[key] = future.result()[1]
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert listing',
                    event='crawler_page_failed',
                    level='warning',
                    url=LIST_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    soups = list(first_pages.values())
    remaining = []
    for (state, year), soup in first_pages.items():
        remaining.extend(
            {'s_sche': state, 's_year': year, 'page': page}
            for page in page_numbers(soup)
            if page > 1
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_page, params): params for params in remaining}
        for future in as_completed(futures):
            try:
                soups.append(future.result()[1])
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert listing page',
                    event='crawler_page_failed',
                    level='warning',
                    url=LIST_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for soup in soups:
        for card in soup.select('.reserve-list > ul > li'):
            record = parse_card(card)
            if record:
                records.append(record)

    unique = {(record['url'], record['date'], record['time_from']): record for record in records}
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class MpycKrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mpyc_kr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='KR',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MpycKrCrawler().run()


if __name__ == '__main__':
    main()
