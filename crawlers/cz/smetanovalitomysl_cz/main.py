import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://smetanovalitomysl.cz/'
PROGRAM_URL = urljoin(SOURCE_URL, 'program/')
EVENTS_API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/udalosti')
SOURCE = 'Smetanova Litomyšl'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/json;q=0.9,*/*;q=0.8',
    'Accept-Language': 'cs,en;q=0.8',
}


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def get_soup(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_id_from_url(url):
    values = parse_qs(urlparse(url).query).get('p', [])
    return int(values[0]) if values and values[0].isdigit() else None


def section_date(section):
    day_link = section.select_one('a[href*="datum="]')
    if day_link:
        value = parse_qs(urlparse(day_link.get('href', '')).query).get('datum', [''])[0]
        if re.fullmatch(r'\d{8}', value):
            try:
                return datetime.strptime(value, '%Y%m%d').date().isoformat()
            except ValueError:
                return None

    heading = clean_text(section.get_text(' ', strip=True))
    match = re.search(r'\b(\d{1,2})\.\s*([\wáčďéěíňóřšťúůýž]+)', heading, re.I)
    year_match = re.search(r'\b(20\d{2})\b', heading)
    if not match or not year_match:
        return None
    months = {
        'ledna': 1, 'února': 2, 'března': 3, 'dubna': 4, 'května': 5,
        'června': 6, 'července': 7, 'srpna': 8, 'září': 9,
        'října': 10, 'listopadu': 11, 'prosince': 12,
    }
    month = months.get(match.group(2).lower())
    if not month:
        return None
    try:
        return datetime(int(year_match.group(1)), month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def fetch_event_details(session, event_ids):
    details = {}
    ids = sorted(set(event_ids))
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        response = session.get(
            EVENTS_API_URL,
            params={
                'include': ','.join(str(event_id) for event_id in chunk),
                'per_page': len(chunk),
                '_fields': 'id,link,content',
            },
            timeout=30,
        )
        response.raise_for_status()
        for item in response.json():
            soup = BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser')
            for unwanted in soup.select('script, style, img, figure'):
                unwanted.decompose()
            details[item['id']] = {
                'url': item.get('link'),
                'description': clean_text(soup.get_text('\n', strip=True)) or None,
            }
    return details


def parse_listing(soup):
    rows = []
    for section in soup.select('.wx-kalendar-den-section'):
        date = section_date(section)
        if not date:
            continue
        for card in section.select('.main-program-cover1'):
            link = card.select_one('a[href*="post_type=udalosti"]')
            title_node = card.select_one('.main-program-title')
            venue_node = card.select_one('.main-program-place')
            if not link or not title_node or not venue_node:
                continue
            event_id = event_id_from_url(link.get('href', ''))
            if event_id is None:
                continue

            title = clean_text(title_node.get_text(' ', strip=True))
            title = clean_text(re.sub(r'\s*/\s*\d{1,2}[.:]\d{2}.*$', '', title))
            venue = clean_text(venue_node.get_text(' ', strip=True)).lstrip('/').strip()
            time_node = card.select_one('.main-program-start')
            time_match = re.search(r'(\d{1,2})[.:](\d{2})', clean_text(
                time_node.get_text(' ', strip=True) if time_node else ''
            ))
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
            if not title or not venue:
                continue
            rows.append({
                'event_id': event_id,
                'title': title,
                'date': date,
                'time_from': time_from,
                'venue': venue,
            })
    return rows


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    rows = parse_listing(get_soup(session, PROGRAM_URL))
    try:
        details = fetch_event_details(session, [row['event_id'] for row in rows])
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch event descriptions',
            event='crawler_detail_batch_failed',
            level='warning',
            url=EVENTS_API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        details = {}

    concerts = []
    for row in rows:
        event_id = row.pop('event_id')
        detail = details.get(event_id, {})
        url = detail.get('url')
        if not url:
            # The WordPress query URL is a stable, valid event detail URL too.
            url = urljoin(SOURCE_URL, f'?post_type=udalosti&p={event_id}')
        concerts.append({
            **row,
            'url': url,
            'city': 'Litomyšl',
            'country_code': 'CZ',
            'description': detail.get('description'),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return concerts


class SmetanovaLitomyslCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='smetanovalitomysl_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
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
    SmetanovaLitomyslCrawler().run()


if __name__ == '__main__':
    main()
