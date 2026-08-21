import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ocp.org.pt/'
EVENTS_URL = urljoin(SOURCE_URL, 'eventos/?lg=pt')
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
SOURCE = 'Orquestra de Câmara Portuguesa'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'janeiro': 1, 'fevereiro': 2, 'março': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8,
    'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, 'lg=pt', ''))


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(container):
    day = clean_text(container.select_one('.day-nr h1'))
    values = [clean_text(item) for item in container.select('.month-year p')]
    if len(values) != 2 or values[0].casefold() not in MONTHS:
        return None
    try:
        return date(int(values[1]), MONTHS[values[0].casefold()], int(day)).isoformat()
    except (TypeError, ValueError):
        return None


def split_location(location):
    parts = [part.strip() for part in location.rsplit(',', 1)]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return location.strip(), ''


def city_from_address(address):
    # Portuguese addresses commonly end in "NNNN-NNN Locality".
    match = re.search(r'\b\d{4}-\d{3}\s+([^,\n]+)', address)
    if match:
        return re.sub(r'\s+(?:Ver no Mapa|Portugal).*$', '', match.group(1), flags=re.I).strip()
    return ''


def parse_listing_article(article):
    link = article.select_one('a[href*="/eventos/"]')
    title_node = article.select_one('.info h2')
    location_node = article.select_one('.event-location')
    event_date = parse_date(article)
    title = clean_text(title_node)
    location = clean_text(location_node)
    venue, city = split_location(location)
    if not link or not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(link.get('href', '')),
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': 'PT',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def feed_max_page(session):
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    match = re.search(r'"max_page":"(\d+)"', response.text)
    if not match:
        raise ValueError('Could not determine event feed pagination')
    return int(match.group(1))


def fetch_feed(session, order, max_page):
    records = []
    seen_urls = set()
    for page in range(1, max_page + 1):
        response = session.post(
            AJAX_URL,
            data={
                'action': 'loadmore_events',
                'page': page,
                'max_page': max_page,
                'lg': 'pt',
                'entryType': '',
                'entryOrder': order,
                'isFilter': 'filter',
                'date': date.today().strftime('%Y%m%d'),
            },
            headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': EVENTS_URL},
            timeout=45,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('article.event')
        if not articles:
            break
        new_count = 0
        for article in articles:
            record = parse_listing_article(article)
            if record and record['url'] not in seen_urls:
                seen_urls.add(record['url'])
                records.append(record)
                new_count += 1
        # The endpoint trusts the client-supplied max_page and can repeat its
        # final page, so record identity is the reliable pagination boundary.
        if not new_count:
            break
    return records


def enrich_record(session, record):
    soup = get_soup(session, record['url'])
    intro = soup.select_one('#intro-event')
    if not intro:
        return record

    time_text = clean_text(intro.select_one('.time strong'))
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', time_text)
    record['time_from'] = f'{int(match.group(1)):02d}:{match.group(2)}' if match else None

    detail_location = clean_text(intro.select_one('.event-location'))
    venue, city = split_location(detail_location)
    if venue:
        record['venue'] = venue
    address = clean_text(intro.select_one('.address strong'))
    record['city'] = city or record['city'] or city_from_address(address)

    description_parts = []
    for section in soup.select('main section.text-block'):
        text = clean_text(section)
        if text and text not in description_parts:
            description_parts.append(text)
    record['description'] = '\n\n'.join(description_parts) or None
    return record


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    max_page = feed_max_page(session)
    records = fetch_feed(session, 'ASC', max_page) + fetch_feed(session, 'DESC', max_page)

    unique = {record['url']: record for record in records}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(enrich_record, session, record): record for record in unique.values()}
        for future in as_completed(futures):
            record = futures[future]
            try:
                future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    valid = [record for record in unique.values() if record['city'] and record['venue']]
    return sorted(valid, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OcpOrgPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ocp_org_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OcpOrgPtCrawler().run()


if __name__ == '__main__':
    main()
