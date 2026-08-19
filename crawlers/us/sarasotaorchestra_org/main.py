import re
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sarasotaorchestra.org/'
SOURCE = 'Sarasota Orchestra'
CITY = 'Sarasota'

SERIES_PAGES = (
    'subscriptions/masterworks',
    'subscriptions/discoveries',
    'subscriptions/chamber',
    'subscriptions/pops',
)
LINK_PAGES = (
    '',
    'education/programs/youth-orchestras/concert-schedule',
)
EXCLUDED_PATH_PARTS = (
    '/box-office-information',
    '/concert-extras/',
    '/gift-certificates',
    '/special-offers-and-discounts',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*([ap])\.?m\.?', clean_text(value), re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{minute}'


def exact_search_result(session, title, category):
    search_url = urljoin(SOURCE_URL, f'component/finder/search?q={quote(title)}')
    response = session.get(search_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    prefix = f'/concerts/{category}/'
    for link in soup.select('a[href]'):
        if clean_text(link).casefold() != title.casefold():
            continue
        url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
        if prefix in url:
            return url
    return None


def discover_urls(session):
    urls = set()

    for path in SERIES_PAGES:
        response = session.get(urljoin(SOURCE_URL, path), timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        category = path.rsplit('/', 1)[-1]
        for listing in soup.select('.concert-listing-container'):
            heading = listing.select_one('h3')
            if not heading:
                continue
            title = clean_text(heading).split('|', 1)[-1].strip()
            if not title:
                continue
            result = exact_search_result(session, title, category)
            if result:
                urls.add(result)
            else:
                log_message(
                    'Concert detail page not found',
                    event='crawler_detail_not_found',
                    level='warning',
                    url=response.url,
                    title=title,
                )

    for path in LINK_PAGES:
        response = session.get(urljoin(SOURCE_URL, path), timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('a[href*="/concerts/"]'):
            url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
            if not any(part in url for part in EXCLUDED_PATH_PARTS):
                urls.add(url)

    return sorted(urls)


def description_from_page(soup):
    parts = []
    program = soup.select_one('.row.program')
    about_heading = next(
        (heading for heading in soup.select('h2') if clean_text(heading).casefold() == 'about'),
        None,
    )
    about = about_heading.parent if about_heading else None
    for node in (program, about):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title_node = soup.select_one('main h1, .com-content-article h1, h1')
    title = clean_text(title_node)
    description = description_from_page(soup)
    records = []

    for occurrence in soup.select('[itemprop="subEvent"]'):
        date_node = occurrence.select_one('[itemprop="startDate"]')
        time_node = occurrence.select_one('[itemprop="doorTime"]')
        venue_node = occurrence.select_one('[itemprop="location"] [itemprop="name"]')
        address_node = occurrence.select_one('[itemprop="location"] [itemprop="address"]')
        event_date = (date_node.get('content') if date_node else '') or ''
        venue = clean_text(venue_node.get('content') if venue_node else '')
        address = clean_text(address_node.get('content') if address_node else '')
        city = CITY if re.search(r'\bSarasota\b', address, re.I) else ''

        if not title or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', event_date):
            continue
        if not venue or not city:
            log_message(
                'Skipping occurrence without a supported venue or city',
                event='crawler_invalid_occurrence',
                level='warning',
                url=url,
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(time_node.get('content') if time_node else ''),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    urls = discover_urls(session)
    for url in urls:
        records.extend(parse_detail_page(session, url))

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SarasotaOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sarasotaorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        return scrape_concerts()


def main():
    SarasotaOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
