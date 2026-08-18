import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lagrangesymphony.org/'
SOURCE = 'LaGrange Symphony Orchestra'
CURRENT_SEASON_URL = urljoin(SOURCE_URL, 'currentseason/')
REVIEWS_URL = urljoin(SOURCE_URL, 'overview/season-reviews/')
DEFAULT_VENUE = 'Callaway Auditorium'
DEFAULT_CITY = 'LaGrange'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(\d{1,2}),\s+(20\d{2})\b',
    re.IGNORECASE,
)


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return ''
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return ''


def make_record(title, event_date, url, description=None, time_from=None,
                venue=DEFAULT_VENUE, city=DEFAULT_CITY):
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': clean_text(title),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': clean_text(description, '\n') or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def current_season_records(soup):
    schedule = {}
    for link in soup.select('a[href^="#"]'):
        text = clean_text(link)
        event_date = parse_date(text)
        anchor = link.get('href', '')[1:]
        if event_date and anchor and anchor != 'currentseasontickets':
            title = clean_text(DATE_RE.sub('', text)).strip(' -–—')
            if title:
                schedule.setdefault(anchor, (title, event_date))

    records = []
    event_ids = set(schedule)
    for anchor, (title, event_date) in schedule.items():
        section = soup.find(id=anchor)
        description_parts = []
        node = section.find_next_sibling() if section else None
        while node is not None:
            if node.get('id') in event_ids:
                break
            text = clean_text(node, '\n')
            if text:
                description_parts.append(text)
            node = node.find_next_sibling()
        record = make_record(
            title,
            event_date,
            f'{CURRENT_SEASON_URL}#{anchor}',
            '\n\n'.join(description_parts),
        )
        if record:
            records.append(record)

    page_text = clean_text(soup, '\n')
    special = re.search(
        r'Special Event\s+Join us for\s+(.+?)\s*,\s+on\s+'
        r'((?:January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+\d{1,2},\s+20\d{2}),\s+'
        r'in\s+([^,\n]+),\s+[A-Z]{2},(.*?)(?=MORE ABOUT THE CURRENT SEASON)',
        page_text,
        re.IGNORECASE | re.DOTALL,
    )
    if special:
        details = special.group(4)
        venue_match = re.search(r'\bhistoric\s+([^.,\n]*?(?:Theater|Theatre))\b', details, re.I)
        ticket_link = soup.find('a', href=re.compile(r'/Productions/\d+'))
        url = ticket_link.get('href') if ticket_link else CURRENT_SEASON_URL
        record = make_record(
            special.group(1),
            parse_date(special.group(2)),
            url,
            special.group(0),
            venue=clean_text(venue_match.group(1)) if venue_match else '',
            city=clean_text(special.group(3)),
        )
        if record:
            records.append(record)
    return records


def review_links(session):
    links = []
    page = 1
    while True:
        url = REVIEWS_URL if page == 1 else urljoin(REVIEWS_URL, f'page/{page}/')
        response = session.get(url, timeout=45)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('article.type-post')
        if not articles:
            break
        for article in articles:
            title_link = article.select_one('.entry-title a[href]')
            if not title_link:
                continue
            title_text = clean_text(title_link)
            event_date = parse_date(title_text)
            if not event_date:
                continue
            title = clean_text(DATE_RE.sub('', title_text)).strip(' ,:-–—')
            href = urljoin(url, title_link.get('href'))
            if title and urlparse(href).netloc.endswith('lagrangesymphony.org'):
                links.append((title, event_date, href))
        next_link = soup.find('a', string=re.compile(r'^\s*NEXT\s*$', re.I))
        if not next_link:
            break
        page += 1
    return links


def review_record(session, title, event_date, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('.entry-content') or soup.select_one('article.type-post')
    description = clean_text(content, '\n') if content else None
    return make_record(title, event_date, url, description)


class LagrangeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lagrangesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        response = session.get(CURRENT_SEASON_URL, timeout=45)
        response.raise_for_status()
        records = current_season_records(BeautifulSoup(response.text, 'html.parser'))

        for title, event_date, url in review_links(session):
            try:
                record = review_record(session, title, event_date, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Could not fetch LaGrange Symphony concert review',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        if not records:
            log_message(
                'No LaGrange Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=CURRENT_SEASON_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


def main():
    LagrangeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
