import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-lausanne.ch/'
ARCHIVES_URL = urljoin(SOURCE_URL, 'archives/')
SOURCE = 'Opéra de Lausanne'
CITY = 'Lausanne'
VENUE = 'Opéra de Lausanne'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.7',
}

FRENCH_MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def same_site_url(href):
    url = urljoin(SOURCE_URL, href or '')
    if urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
        return ''
    return url.split('#', 1)[0]


def catalogue_urls(session):
    archives = get_soup(session, ARCHIVES_URL)
    pages = {ARCHIVES_URL}
    for link in archives.select('a[href]'):
        url = same_site_url(link.get('href'))
        if re.search(r'/(?:season/\d{4}-\d{2}|saison-\d{4}-\d{4})/?$', url):
            pages.add(url)

    # The new calendar URL can be linked only from the navigation, while the
    # historical archive cards use the older /saison-YYYY-YYYY/ form.
    for link in archives.select('a[href*="/season/"]'):
        url = same_site_url(link.get('href'))
        if url:
            pages.add(url)
    return sorted(pages)


def show_urls(session):
    urls = set()
    for page_url in catalogue_urls(session):
        try:
            soup = get_soup(session, page_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape catalogue page',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('a[href*="/show/"]'):
            url = same_site_url(link.get('href'))
            if re.search(r'/show/[^/]+/?$', url):
                urls.add(url)
    return sorted(urls)


def parse_date(node):
    day = clean_text(node.select_one('.date-booking__number'))
    month_year = clean_text(node.select_one('.date-booking__month')).lower()
    match = re.fullmatch(r'(\S+)\s+(\d{4})', month_year)
    if not day.isdigit() or not match:
        return None
    month = FRENCH_MONTHS.get(match.group(1))
    if not month:
        return None
    try:
        return date(int(match.group(2)), month, int(day)).isoformat()
    except ValueError:
        return None


def page_title(soup):
    heading = soup.select_one('h1')
    title = clean_text(heading)
    if not title:
        meta = soup.select_one('meta[property="og:title"]')
        title = clean_text(meta.get('content')) if meta else ''
    return title


def page_description(soup):
    parts = []
    composer = clean_text(soup.select_one('.show-header__composer, .show-hero__composer'))
    if composer:
        parts.append(composer)
    for node in soup.select('#intro .content__text, .js-show-content .content__text'):
        value = clean_text(node)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def detail_records(session, url):
    soup = get_soup(session, url)
    title = page_title(soup)
    description = page_description(soup)
    if not title:
        return []

    records = []
    for booking in soup.select('#showdates .date-booking'):
        event_date = parse_date(booking)
        if not event_date:
            continue
        time_value = clean_text(booking.select_one('.date-booking__time'))
        time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', time_value)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_match.group(0) if time_match else None,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'CH',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = show_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_records, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
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
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class OperaLausanneChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_lausanne_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
    OperaLausanneChCrawler().run()


if __name__ == '__main__':
    main()
