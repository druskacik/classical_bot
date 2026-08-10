import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-hof.de/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Theater Hof'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

HOME_VENUES = {
    'großes haus': 'Theater Hof – Großes Haus',
    'studio': 'Theater Hof – Studio',
    "mocky's backstage bistro": "Mocky's Backstage Bistro, Theater Hof",
    'im und um das theater hof': 'Theater Hof',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def sitemap_pages(session):
    root = ElementTree.fromstring(get_response(session, SITEMAP_URL).content)
    namespace = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    child_urls = [node.text for node in root.findall('.//sm:sitemap/sm:loc', namespace)]
    pages = []
    for child_url in child_urls:
        child = ElementTree.fromstring(get_response(session, child_url).content)
        pages.extend(node.text for node in child.findall('.//sm:url/sm:loc', namespace))
    return pages


def resolve_location(value):
    location = clean_text(value).strip(' ,')
    home_venue = HOME_VENUES.get(location.lower())
    if home_venue:
        return home_venue, 'Hof'

    if location.lower() == 'großer sitzungssaal im hofer rathaus':
        return location, 'Hof'
    if location.lower() == 'rosenthal-theater selb':
        return location, 'Selb'

    # Touring performances use "City, Venue" on the site. Preserve the
    # explicit location rather than applying Theater Hof's home defaults.
    if ',' in location:
        city, venue = (part.strip() for part in location.split(',', 1))
        if city and venue:
            return venue, city
    return None


def parse_listing_item(item, page_url):
    link = item.select_one('.termin-title a[href*="/spielplan/stuecke/detail/"]')
    info = item.select_one('.termin-info')
    location_node = item.select_one('.termin-info .location')
    if not link or not info or not location_node:
        return None

    datetime_match = re.search(
        r'\b(\d{1,2})\.(\d{1,2})\.(20\d{2}),\s*'
        r'(\d{1,2}):(\d{2})\s*Uhr\b',
        clean_text(info),
        re.I,
    )
    if not datetime_match:
        return None
    try:
        event_date = date(
            int(datetime_match.group(3)),
            int(datetime_match.group(2)),
            int(datetime_match.group(1)),
        ).isoformat()
    except ValueError:
        return None

    location = resolve_location(location_node)
    title = clean_text(link)
    url = urljoin(page_url, link.get('href', '').strip())
    if not title or not url or not location:
        return None
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{int(datetime_match.group(4)):02d}:{datetime_match.group(5)}',
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    parts = []
    author = clean_text(soup.select_one('.infobox-author'))
    information = clean_text(soup.select_one('.stueck-detail-info .info-text'))
    if author:
        parts.append(author)
    if information and information not in parts:
        parts.append(information)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    pages = sitemap_pages(session)
    month_pattern = re.compile(
        r'/spielplan/(?:januar|februar|maerz|april|mai|juni|juli|august|'
        r'september|oktober|november|dezember)-20\d{2}/?$'
    )
    month_urls = sorted(url for url in pages if month_pattern.search(url))
    records = []
    for url in month_urls:
        soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
        for item in soup.select('.termine-list .termin-item'):
            record = parse_listing_item(item, url)
            if record:
                records.append(record)

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    records = list(unique.values())
    by_url = {record['url'] for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in by_url}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Theater Hof production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record['url'])
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class TheaterHofDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_hof_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterHofDeCrawler().run()


if __name__ == '__main__':
    main()
