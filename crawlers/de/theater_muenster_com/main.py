import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-muenster.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'spielplan/kalender')
SOURCE = 'Theater Münster'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The calendar includes a small number of touring performances. All other
# named venues in this municipal theatre calendar are in Münster.
EXTERNAL_CITIES = {
    'bonn': 'Bonn',
    'gescher': 'Gescher',
    'hameln': 'Hameln',
    'mettingen': 'Mettingen',
    'mülheim': 'Mülheim an der Ruhr',
    'muenchen': 'München',
    'münchen': 'München',
    'oberhausen': 'Oberhausen',
    'osnabrück': 'Osnabrück',
    'recklinghausen': 'Recklinghausen',
}
INVALID_VENUES = {'*', 'ausserhalb', 'mobil', 'ort wird noch bekanntgegeben'}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def city_for_venue(venue):
    lowered = venue.casefold()
    for fragment, city in EXTERNAL_CITIES.items():
        if fragment in lowered:
            return city
    if lowered in INVALID_VENUES:
        return None
    return 'Münster'


def calendar_months(today=None):
    """Return the previous, current, and next theatre seasons (August-July)."""
    today = today or date.today()
    season_year = today.year if today.month >= 8 else today.year - 1
    months = []
    for start_year in range(season_year - 1, season_year + 2):
        for offset in range(12):
            month_number = 8 + offset
            year = start_year + (month_number - 1) // 12
            month = (month_number - 1) % 12 + 1
            months.append(f'{year:04d}-{month:02d}')
    return months


def parse_calendar(soup, year_month):
    records = []
    for event in soup.select('.tm-performance'):
        link = event.select_one('.tm-performance__productionName a[href]')
        day_node = event.select_one('.tm-performance__dayNumber')
        venue = clean_text(event.select_one('.tm-performance__location'))
        title = clean_text(link)
        day_match = re.search(r'\b(\d{1,2})\b', clean_text(day_node))
        city = city_for_venue(venue) if venue else None
        if not link or not title or not day_match or not venue or not city:
            continue

        try:
            event_date = date.fromisoformat(f'{year_month}-{int(day_match.group(1)):02d}')
        except ValueError:
            continue

        time_text = clean_text(event.select_one('.tm-performance__performanceTime'))
        time_match = re.search(r'\b(\d{1,2})[.:](\d{2})\b', time_text)
        summary = clean_text(event.select_one('.tm-performance__productionInfo')) or None
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': urljoin(SOURCE_URL, link['href']),
            'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
            'venue': venue,
            'city': city,
            'country_code': 'DE',
            'description': summary,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(soup, fallback=None):
    parts = []
    for selector in (
        '.tm-autoProductionTitleModule__author',
        '.tm-textModule__body',
    ):
        parts.extend(clean_text(node) for node in soup.select(selector))
    unique_parts = []
    for part in parts:
        if part and part not in unique_parts:
            unique_parts.append(part)
    return clean_text('\n\n'.join(unique_parts)) or fallback


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records_by_key = {}

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, CALENDAR_URL, {'date': month}): month
            for month in calendar_months()
        }
        for future in as_completed(futures):
            month = futures[future]
            try:
                month_records = parse_calendar(future.result(), month)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_URL}?date={month}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for record in month_records:
                key = (record['title'], record['date'], record['time_from'], record['venue'])
                records_by_key[key] = record

    records = list(records_by_key.values())
    records_by_url = {}
    for record in records:
        records_by_url.setdefault(record['url'], []).append(record)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in records_by_url}
        for future in as_completed(futures):
            url = futures[future]
            try:
                description = detail_description(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if description:
                for record in records_by_url[url]:
                    record['description'] = description

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class TheaterMuensterComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_muenster_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
    TheaterMuensterComCrawler().run()


if __name__ == '__main__':
    main()
