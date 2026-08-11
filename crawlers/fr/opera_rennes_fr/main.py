import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-rennes.fr/fr'
PROGRAMME_URL = f'{SOURCE_URL}/programmation'
SOURCE = 'Opéra de Rennes'
DEFAULT_CITY = 'Rennes'
DEFAULT_VENUE = 'Opéra de Rennes'

# The calendar is mixed.  These first-party categories are the plausible
# performance feed; ambiguous entries are deliberately sent to potential_event.
CATEGORY_IDS = ('28', '14', '517', '457', '551', '47', '74', '42', '340')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11,
    'décembre': 12, 'decembre': 12,
}
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def season_ids(soup):
    select = soup.select_one('select[name="season"]')
    if not select:
        return []
    return list(dict.fromkeys(
        option.get('value', '').strip()
        for option in select.select('option[value]')
        if option.get('value', '').strip() and option.get('value') != 'All'
    ))


def filter_params(season_id, page=0):
    params = [(f'categories[{value}]', value) for value in CATEGORY_IDS]
    params.extend((('season', season_id), ('page', page)))
    return params


def detail_urls(session):
    first = get_soup(session, PROGRAMME_URL)
    urls = []
    for season_id in season_ids(first):
        page = 0
        while True:
            url = f'{PROGRAMME_URL}?{urlencode(filter_params(season_id, page))}'
            soup = get_soup(session, url)
            page_urls = list(dict.fromkeys(
                urljoin(SOURCE_URL, link.get('href'))
                for link in soup.select('main a[href*="/fr/evenement/"]')
            ))
            if not page_urls:
                break
            urls.extend(page_urls)
            next_page = page + 1
            if not soup.select_one(f'a[href*="page={next_page}"]'):
                break
            page = next_page
    return list(dict.fromkeys(urls))


def description_from(soup):
    parts = []
    for selector in (
        '.field--name-field-intro',
        '.field--name-field-presentation',
        '.field--name-field-distribution',
        '.field--name-field-production',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def location_from(soup):
    body = soup.select_one('.field--name-body')
    body_text = clean_text(body)
    if 'hors les murs' not in body_text.lower():
        return DEFAULT_VENUE, DEFAULT_CITY, 'FR'

    elements = body.select('p, div') if body else []
    marker_seen = False
    for element in elements:
        text = clean_text(element)
        if not text:
            continue
        if 'hors les murs' in text.lower():
            marker_seen = True
            continue
        if not marker_seen:
            continue
        if text.lower() == 'le triangle, cité de la danse':
            return 'Le Triangle, cité de la danse', DEFAULT_CITY, 'FR'
        match = re.match(r'(.+?),\s*([^,]+)$', text)
        if match:
            venue, city = (part.strip(' .') for part in match.groups())
            if venue and city and not re.search(r'\d', city):
                return venue, city, 'FR'
    return None, None, None


def occurrence_years(soup):
    body_text = clean_text(soup.select_one('.field--name-body'))
    return [int(value) for value in dict.fromkeys(re.findall(r'\b20\d{2}\b', body_text))]


def choose_year(month, years):
    if not years:
        return None
    if len(years) == 1:
        return years[0]
    return min(years) if month >= 7 else max(years)


def parse_occurrences(soup):
    field = soup.select_one('.field--name-field-dates-text')
    years = occurrence_years(soup)
    if not field or not years:
        return []

    rows = field.select('p, li') or [field]
    occurrences = []
    for row in rows:
        text = clean_text(row).lower()
        if not re.search(MONTH_PATTERN, text):
            continue
        month_matches = list(re.finditer(MONTH_PATTERN, text))
        # A row normally has one month, possibly shared by several named days.
        month = MONTHS[month_matches[-1].group(0)]
        explicit_year = re.search(r'\b(20\d{2})\b', text)
        year = int(explicit_year.group(1)) if explicit_year else choose_year(month, years)
        if not year:
            continue
        prefix = text[:month_matches[-1].start()]
        days = [int(value) for value in re.findall(r'\b([0-3]?\d)\b', prefix)]
        days = [value for value in days if 1 <= value <= 31]
        times = [
            f'{int(hour):02d}:{minute or "00"}'
            for hour, minute in re.findall(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?', text)
        ]
        for day in dict.fromkeys(days):
            try:
                event_date = date(year, month, day).isoformat()
            except ValueError:
                continue
            # Multiple times with one date are distinct performances.  When a
            # row lists several dates, a single time applies to each date.
            row_times = times if len(days) == 1 or len(times) <= 1 else [None]
            for start_time in row_times or [None]:
                occurrences.append((event_date, start_time))
    return list(dict.fromkeys(occurrences))


def parse_detail(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('h1'))
    venue, city, country_code = location_from(soup)
    if not title or not venue or not city:
        return []
    description = description_from(soup)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': start_time,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, start_time in parse_occurrences(soup)
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = detail_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(parse_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opera de Rennes event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class OperaRennesFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_rennes_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaRennesFrCrawler().run()


if __name__ == '__main__':
    main()
