import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivalpianistico.it/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'il-festival/archivio/')
SOURCE = 'Festival Pianistico Internazionale di Brescia e Bergamo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

ITALIAN_MONTHS = {
    'gennaio': 1,
    'febbraio': 2,
    'marzo': 3,
    'aprile': 4,
    'maggio': 5,
    'giugno': 6,
    'luglio': 7,
    'agosto': 8,
    'settembre': 9,
    'ottobre': 10,
    'novembre': 11,
    'dicembre': 12,
}


def clean_text(value):
    if value is None:
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


def parse_italian_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(ITALIAN_MONTHS) + r')\s+(\d{4})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime(
            int(match.group(3)),
            ITALIAN_MONTHS[match.group(2).casefold()],
            int(match.group(1)),
        ).date().isoformat()
    except ValueError:
        return None


def parse_detail(soup, url):
    title_node = soup.select_one('h1')
    date_node = soup.select_one('.dettaglio-concerto-data')
    city_node = soup.select_one('.dettaglio-concerto-bs-bg-data > div:first-child')
    location_node = soup.select_one('.dettaglio-concerto-specifiche-luogo-fisico-ora')
    if not all((title_node, date_node, city_node, location_node)):
        return None

    title = clean_text(title_node).replace('\n', ' ')
    event_date = parse_italian_date(clean_text(date_node))
    city = clean_text(city_node).replace('\n', ' ')

    venue_node = location_node.select_one('strong') or location_node.select_one('span')
    venue = clean_text(venue_node).replace('\n', ' ') if venue_node else ''
    if not venue:
        venue = re.split(r'\s*-?\s*ore\s+\d', clean_text(location_node), maxsplit=1, flags=re.I)[0]

    time_match = re.search(r'\bore\s+(\d{1,2})[.:](\d{2})\b', clean_text(location_node), re.I)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description = clean_text(soup.select_one('.concerto-singolo-testo-descrittivo')) or None
    if not all((title, event_date, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def edition_urls(archive_soup):
    urls = set()
    for link in archive_soup.select('a[href*="/festival/edizione-"]'):
        href = urljoin(SOURCE_URL, link.get('href', ''))
        if re.search(r'/festival/edizione-\d{4}/?$', href):
            urls.add(href)
    return sorted(urls)


def calendar_url(edition_soup):
    for link in edition_soup.select('a[href]'):
        href = urljoin(SOURCE_URL, link.get('href', ''))
        if re.search(r'/programma(?:-e)?-calendario-\d{4}/?$', href):
            return href
    return None


def concert_urls(calendar_soup):
    return sorted({
        urljoin(SOURCE_URL, link.get('href', ''))
        for link in calendar_soup.select('a[href*="/concerto/"]')
        if link.get('href')
    })


class FestivalPianisticoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalpianistico_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            archive_soup = get_soup(session, ARCHIVE_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Festival Pianistico archive',
                event='crawler_fetch_failed',
                level='error',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = set()
        for edition_url in edition_urls(archive_soup):
            try:
                page_url = calendar_url(get_soup(session, edition_url))
                if page_url:
                    urls.update(concert_urls(get_soup(session, page_url)))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival Pianistico edition',
                    event='crawler_item_failed',
                    level='warning',
                    url=edition_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        for url in sorted(urls):
            try:
                record = parse_detail(get_soup(session, url), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Festival Pianistico concert',
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
    FestivalPianisticoItCrawler().run()


if __name__ == '__main__':
    main()
