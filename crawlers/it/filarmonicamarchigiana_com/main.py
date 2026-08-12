import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicamarchigiana.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'stagione/')
SOURCE = 'FORM - Orchestra Filarmonica Marchigiana'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gen': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mag': 5, 'giu': 6,
    'lug': 7, 'ago': 8, 'set': 9, 'ott': 10, 'nov': 11, 'dic': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, *, data=None):
    if data is None:
        response = session.get(url, timeout=45)
    else:
        response = session.post(url, data=data, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def event_urls(soup):
    urls = []
    for link in soup.select('a[href*="/stagione/"]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        path = urlparse(url).path
        # Occurrence pages always contain a season, category, production, and
        # dated performance slug. This excludes filter and production pages.
        if len([part for part in path.split('/') if part]) < 5:
            continue
        if not re.search(r'/stagione/[^/]+/[^/]+/[^/]+/[^/]*\d{4}[^/]*/?$', path):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def parse_date(node):
    match = re.search(
        r'\b(\d{1,2})\s+([a-z]{3})\s+(\d{4})\b',
        clean_text(node).casefold().replace('\n', ' '),
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def parse_detail(soup, url):
    title = clean_text(soup.select_one('.ev-header h1'))
    date_node = soup.select_one('.main-body .pl-date')
    where = soup.select_one('.main-body .pl-where')
    event_date = parse_date(date_node)
    if not title or not event_date or where is None:
        return None

    spans = where.find_all('span', recursive=False)
    city = clean_text(spans[0]) if spans else ''
    time_match = re.search(r'\bore\s*(\d{1,2})[.:](\d{2})\b', clean_text(where), re.I)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    venue_parts = []
    for child in where.children:
        if getattr(child, 'name', None) == 'span':
            break
        text = clean_text(child)
        if text:
            venue_parts.append(text)
    venue = clean_text(' '.join(venue_parts))
    if not venue or not city:
        return None

    description_parts = []
    header = soup.select_one('.ev-header')
    if header:
        for node in header.select('h2, h6'):
            text = clean_text(node)
            if text:
                description_parts.append(text)
    for selector in ('.evento-presentation', '.evento-note', '.aside-info .evento-cast'):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text and text.casefold() not in {'note', 'interpreti'}:
            description_parts.append(text)

    country_code = 'AT' if city.casefold() in {'vienna', 'wien'} else 'IT'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text('\n\n'.join(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FilarmonicaMarchigianaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicamarchigiana_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            current = get_soup(session, CALENDAR_URL)
            archive = get_soup(
                session,
                CALENDAR_URL,
                data={'src_t': '', 'src_l': '', 'src_a': '1', 'src_d': '', 'src_c': ''},
            )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch FORM calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = event_urls(current)
        for url in event_urls(archive):
            if url not in urls:
                urls.append(url)

        records = []
        for url in urls:
            try:
                record = parse_detail(get_soup(session, url), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch FORM event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FilarmonicaMarchigianaComCrawler().run()


if __name__ == '__main__':
    main()
