import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatrocolon.org.ar/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario/')
SEASON_URL = urljoin(SOURCE_URL, 'temporada/temporada-{year}/')
SOURCE = 'Teatro Colón'
CITY = 'Buenos Aires'
COUNTRY_CODE = 'AR'
FIRST_CALENDAR_YEAR = 2023

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-AR,es;q=0.9,en;q=0.6',
}
_thread_state = threading.local()

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'setiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url, params=None):
    session = getattr(_thread_state, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _thread_state.session = session
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_time(value):
    match = re.search(r'(\d{1,2})[.:](\d{2})', clean_text(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def production_url(node):
    link = node.select_one('a[href*="/produccion/"]')
    return urljoin(SOURCE_URL, link.get('href')) if link and link.get('href') else ''


def calendar_records(year, month):
    soup = get_soup(CALENDAR_URL, params={'a': year, 'mes': month})
    records = []
    for item in soup.select('.calendar-item'):
        day_node = item.select_one('.day-number')
        title_node = item.select_one('h1')
        url = production_url(item)
        day_match = re.search(r'\d{1,2}', clean_text(day_node))
        if not day_match or not title_node or not url:
            continue
        try:
            event_date = date(year, month, int(day_match.group())).isoformat()
        except ValueError:
            continue
        title = clean_text(title_node)
        if not title:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(item.select_one('.day-hour')),
            '_listing_text': clean_text(item.select_one('.event-content')),
        })
    return records


def season_production_urls(year):
    urls = set()
    page = 1
    while True:
        url = SEASON_URL.format(year=year)
        if page > 1:
            url = urljoin(url, f'page/{page}/')
        soup = get_soup(url)
        page_urls = {
            urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
            for link in soup.select('main a[href*="/produccion/"]')
            if link.get('href')
        }
        new_urls = page_urls - urls
        if not new_urls:
            break
        urls.update(new_urls)
        next_link = soup.select_one(f'a[href*="/page/{page + 1}/"]')
        if not next_link:
            break
        page += 1
    return urls


def detail_title(soup):
    node = soup.select_one('.card-ballet h1, main > section h1, main h1')
    if node:
        return clean_text(node)
    title = clean_text(soup.title)
    return re.sub(r'\s+-\s+Teatro Colón\s*$', '', title).strip()


def detail_location(soup):
    place = clean_text(soup.select_one('#the_place'))
    schema_name = soup.select_one('[itemprop="location"] meta[itemprop="name"]')
    schema_name = clean_text(schema_name.get('content')) if schema_name else ''
    venue = place or schema_name
    if not venue:
        return None, None

    # Room names on production pages refer to rooms inside Teatro Colón. Keep
    # explicitly named external venues intact instead of assigning the home hall.
    if re.match(r'^(sala|salón|foyer)\b', venue, re.IGNORECASE):
        venue = f'Teatro Colón – {venue}'

    locality = soup.select_one('[itemprop="addressLocality"]')
    city = clean_text(locality) if locality else CITY
    if 'buenos aires' in city.lower():
        city = CITY
    return venue, city


def detail_description(soup, listing_text=None):
    parts = []
    category = clean_text(soup.select_one('.card-ballet .category'))
    credits = clean_text(soup.select_one('.card-ballet .credits'))
    synopsis = clean_text(soup.select_one('.production-details .sinopsis'))
    for value in (category, credits, synopsis, listing_text):
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def detail_occurrences(soup, year, url):
    title = detail_title(soup)
    if not title:
        return []
    occurrences = []
    for heading in soup.select('.dates .months > .head h4'):
        month = MONTHS.get(clean_text(heading).lower())
        if not month:
            continue
        days = heading.parent.find_next_sibling(class_='month-days')
        if not days:
            continue
        for item in days.select('.day'):
            day_match = re.search(r'\d{1,2}', clean_text(item.select_one('.day-number')))
            if not day_match:
                continue
            try:
                event_date = date(year, month, int(day_match.group())).isoformat()
            except ValueError:
                continue
            occurrences.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(item.select_one('.day-hour')),
                '_listing_text': None,
            })
    return occurrences


def enrich_production(url, records):
    soup = get_soup(url)
    venue, city = detail_location(soup)
    if not venue or not city:
        return []
    output = []
    for record in records:
        description = detail_description(soup, record.pop('_listing_text', None))
        output.append({
            **record,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return output


def scrape_production(url, records, include_detail_occurrences=False, year=None):
    soup = get_soup(url)
    if include_detail_occurrences:
        existing = {(item['date'], item['time_from']) for item in records}
        for item in detail_occurrences(soup, year, url):
            if (item['date'], item['time_from']) not in existing:
                records.append(item)
    return enrich_production(url, records)


def get_concerts():
    current_year = date.today().year
    calendar_years = range(FIRST_CALENDAR_YEAR, current_year + 2)
    listings = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(calendar_records, year, month): (year, month)
            for year in calendar_years
            for month in range(1, 13)
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                listings.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_URL}?a={year}&mes={month}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    # The current calendar month hides elapsed performances. Production pages
    # in the paginated current-season catalogue retain those concrete dates.
    try:
        current_urls = season_production_urls(current_year)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape current season catalogue',
            event='crawler_page_failed',
            level='warning',
            url=SEASON_URL.format(year=current_year),
            error_type=type(error).__name__,
            error_message=str(error),
        )
        current_urls = set()

    records_by_url = {}
    for record in listings:
        records_by_url.setdefault(record['url'], []).append(record)
    for url in current_urls:
        records_by_url.setdefault(url, [])

    records = []
    # Detail pages can be large; cap concurrency to avoid memory spikes in the
    # long historical scrape while keeping the catalogue run practical.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(
                scrape_production,
                url,
                url_records,
                url in current_urls,
                current_year,
            ): url
            for url, url_records in records_by_url.items()
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class TeatroColonOrgArCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrocolon_org_ar',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    TeatroColonOrgArCrawler().run()


if __name__ == '__main__':
    main()
