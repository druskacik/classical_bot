import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.latvijaskoncerti.lv/lv/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalendars/')
SOURCE = 'Latvijas Koncerti'
ARCHIVE_START_YEAR = 2010

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'lv-LV,lv;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def month_range():
    # Tests found populated public archives in 2012 and empty sampled months
    # in 2008-2010. Begin in 2010 to retain a safety margin. Look three years
    # ahead because seasons are announced well in advance.
    today = date.today()
    end_year = today.year + 3
    for year in range(ARCHIVE_START_YEAR, end_year + 1):
        for month in range(1, 13):
            yield year, month


def listing_occurrences(session, year, month):
    soup = get_soup(
        session,
        SOURCE_URL,
        params={'y': year, 'm': f'{month:02d}'},
    )
    calendar = soup.select_one('.calendar')
    if not calendar:
        return []

    occurrences = []
    for day_node in calendar.select('.calendar-week__item:not(.calendar-week__item--inactive)'):
        day_label = day_node.select_one(':scope > a.button, :scope > span')
        if not day_label:
            continue
        match = re.search(r'\d{1,2}', day_label.get_text(' ', strip=True))
        if not match:
            continue
        try:
            event_date = date(year, month, int(match.group())).isoformat()
        except ValueError:
            continue

        urls = {
            urljoin(SOURCE_URL, link['href'])
            for link in day_node.select('a[href*="/aktualitates/pasakums/"]')
        }
        occurrences.extend((event_date, url) for url in urls)
    return occurrences


def parse_location(value):
    text = clean_text(value)
    if not text or ',' not in text:
        return None, None, None
    city, venue = (part.strip(' ,') for part in text.split(',', 1))
    if not city or not venue:
        return None, None, None
    # Latvian calendar city labels commonly use the locative case.
    city_aliases = {
        'Rīgā': 'Rīga',
        'Jūrmalā': 'Jūrmala',
        'Rēzeknē': 'Rēzekne',
        'Rundālē': 'Rundāle',
        'Liepājā': 'Liepāja',
        'Ventspilī': 'Ventspils',
        'Cēsīs': 'Cēsis',
        'Jelgavā': 'Jelgava',
        'Daugavpilī': 'Daugavpils',
        'Siguldā': 'Sigulda',
    }
    city = city_aliases.get(city, city)
    foreign_countries = {
        'Bredfordā': 'GB',
        'Dublinā': 'IE',
        'Gēteborgā': 'SE',
        'Limerikā': 'IE',
        'Londonā': 'GB',
        'Oslo': 'NO',
        'Stokholmā': 'SE',
    }
    return city, venue, foreign_countries.get(city, 'LV')


def occurrence_time_and_location(soup, event_date):
    wanted_day = date.fromisoformat(event_date).day
    sections = soup.select('.calendar-short__data section')
    selected = None
    for section in sections:
        time_node = section.select_one('time')
        match = re.search(r'\d{1,2}', clean_text(time_node))
        if match and int(match.group()) == wanted_day:
            selected = section
            break
    selected = selected or (sections[0] if sections else None)

    time_from = None
    if selected:
        match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', clean_text(selected))
        if match:
            time_from = match.group(0).zfill(5)

    location_node = soup.select_one('.calendar-short__data .location')
    city, venue, country_code = parse_location(location_node)
    return time_from, city, venue, country_code


def make_record(event_date, url, soup):
    title = clean_text(soup.select_one('.article__content h1.node__title'))
    time_from, city, venue, country_code = occurrence_time_and_location(soup, event_date)
    content = soup.select_one('.article__content.text')
    if content:
        content = BeautifulSoup(str(content), 'html.parser')
        for node in content.select('h1.node__title, .article__tag, script'):
            node.decompose()
    description = clean_text(content) or None
    if not title or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    occurrences = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(listing_occurrences, session, year, month): (year, month)
            for year, month in month_range()
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                occurrences.update(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{SOURCE_URL}?y={year}&m={month:02d}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    detail_cache = {}
    urls = {url for _, url in occurrences}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                detail_cache[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for event_date, url in occurrences:
        soup = detail_cache.get(url)
        if soup:
            record = make_record(event_date, url, soup)
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class LatvijaskoncertiLvCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='latvijaskoncerti_lv',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LV',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LatvijaskoncertiLvCrawler().run()


if __name__ == '__main__':
    main()
