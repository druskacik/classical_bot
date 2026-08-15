import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.malmoopera.se/'
SOURCE = 'Malmö Opera'
ARCHIVE_URL = urljoin(SOURCE_URL, 'forestallningar/arkiv')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1, 'februari': 2, 'mars': 3, 'april': 4,
    'maj': 5, 'juni': 6, 'juli': 7, 'augusti': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}
MONTH_ABBREVIATIONS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'maj': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit(('https', 'www.malmoopera.se', parts.path.rstrip('/'), '', ''))


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def page_count(soup):
    pages = [0]
    for link in soup.select('a[href*="page="]'):
        match = re.search(r'(?:\?|&)page=(\d+)', link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))
    return max(pages) + 1


def performance_links(soup):
    links = set()
    for link in soup.select('a[href^="/forestallningar/"]'):
        path = link.get('href', '').split('?', 1)[0].rstrip('/')
        if path and path != '/forestallningar/arkiv':
            links.add(canonical_url(path))
    return links


def discover_urls(session):
    urls = set()
    for base_url in (SOURCE_URL, ARCHIVE_URL):
        first = get_soup(session, base_url)
        urls.update(performance_links(first))
        for page in range(1, page_count(first)):
            soup = get_soup(session, f'{base_url}?page={page}')
            urls.update(performance_links(soup))
    return urls


def metadata(soup):
    values = {}
    for term in soup.select('dt'):
        definition = term.find_next_sibling('dd')
        if definition:
            values[clean_text(term).casefold()] = clean_text(definition)
    return values


def resolve_location(scene):
    scene = clean_text(scene)
    if not scene:
        return None, None

    # Touring pages sometimes name the host and town in the scene field.
    if ',' in scene:
        venue, city = (part.strip() for part in scene.rsplit(',', 1))
        if venue and city and 'turné' not in city.casefold():
            return venue, city

    # A generic touring label does not identify which town hosts an occurrence.
    if 'turné' in scene.casefold():
        return None, None
    return scene, 'Malmö'


def parse_instance_date(value, year, month):
    match = re.search(
        r'\b(\d{1,2})\s+(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)'
        r'\s+(\d{1,2})[.:](\d{2})\b',
        clean_text(value).casefold(),
    )
    if not match:
        return None, None
    day, month_name, hour, minute = match.groups()
    parsed_month = MONTH_ABBREVIATIONS[month_name]
    if month and parsed_month != month:
        return None, None
    try:
        date = datetime(year, parsed_month, int(day)).date().isoformat()
    except (TypeError, ValueError):
        return None, None
    return date, f'{int(hour):02d}:{minute}'


def detail_records(session, url):
    soup = get_soup(session, f'{url}?items_per_page=All')
    title = clean_text(soup.select_one('h1'))
    values = metadata(soup)
    venue, city = resolve_location(values.get('scen'))
    if not all((title, venue, city)):
        return []

    article = soup.select_one('main article')
    description = clean_text(article) or None
    records = []
    year = month = None
    calendar = soup.select_one('#performance-calendar')
    if not calendar:
        return records

    for element in calendar.select('.tag, .mo-instance'):
        if 'mo-instance' not in (element.get('class') or []):
            match = re.fullmatch(
                r'(januari|februari|mars|april|maj|juni|juli|augusti|september|oktober|november|december)\s+(\d{4})',
                clean_text(element).casefold(),
            )
            if match:
                month = MONTHS[match.group(1)]
                year = int(match.group(2))
            continue
        if year is None:
            continue
        date, time_from = parse_instance_date(element.get_text(' ', strip=True), year, month)
        if not date:
            continue
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'SE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MalmooperaSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='malmoopera_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = discover_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(detail_records, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Malmö Opera performance detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    MalmooperaSeCrawler().run()


if __name__ == '__main__':
    main()
