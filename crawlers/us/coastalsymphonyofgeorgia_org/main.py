import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.coastalsymphonyofgeorgia.org/'
CONCERTS_URL = f'{SOURCE_URL}ticketsaug3'
SOURCE = 'Coastal Symphony of Georgia'
VENUE = 'Center for the Arts'
CITY = 'Brunswick'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*$'
)
TICKET_RE = re.compile(r'purplepass\.com/symphony(?P<season>\d{4})(?P<order>\d+)$')


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def parse_heading(value):
    text = clean_text(value)
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group('date'), '%B %d, %Y').date()
    except ValueError:
        return None
    title = text[:match.start()].strip(' -–—')
    if not title:
        return None
    return title, event_date.isoformat()


def ticket_blocks(section):
    """Return numbered season ticket URLs and their following repertoire."""
    blocks = []
    for link in section.select('a[href]'):
        match = TICKET_RE.search(link.get('href', '').rstrip('/'))
        if not match:
            continue

        repertoire = []
        for node in link.find_all_next(['a', 'h1', 'h2', 'h3', 'p']):
            if node.find_parent('section') is not section:
                break
            if node.name == 'a' and TICKET_RE.search(node.get('href', '').rstrip('/')):
                break
            if node.name in {'h1', 'h2', 'h3'} and parse_heading(node.get_text(' ', strip=True)):
                break
            if node.name == 'p':
                text = clean_text(node.get_text(' ', strip=True))
                lower = text.lower()
                is_marketing = (
                    'ticket' in lower
                    or 'newsletter' in lower
                    or 'event announcements' in lower
                    or 'early access' in lower
                )
                if text and not is_marketing and text not in repertoire:
                    repertoire.append(text)

        blocks.append({
            'season': int(match.group('season')),
            'order': int(match.group('order')),
            'url': link['href'],
            'description': '\n'.join(repertoire) or None,
        })
    return blocks


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CONCERTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    headings = []
    for node in soup.select('h1, h2, h3'):
        parsed = parse_heading(node.get_text(' ', strip=True))
        if parsed:
            headings.append((*parsed, node.find_parent('section')))

    records = []
    for section in {item[2] for item in headings}:
        section_headings = sorted(
            ((title, date) for title, date, parent in headings if parent is section),
            key=lambda item: item[1],
        )
        blocks = sorted(ticket_blocks(section), key=lambda item: (item['season'], item['order']))
        if len(section_headings) != len(blocks):
            log_message(
                'Concert headings and ticket blocks do not match',
                event='crawler_parse_mismatch',
                level='warning',
                url=CONCERTS_URL,
                heading_count=len(section_headings),
                ticket_count=len(blocks),
            )
            continue

        for (title, event_date), block in zip(section_headings, blocks):
            records.append({
                'title': title,
                'date': event_date,
                'url': block['url'],
                'time_from': None,
                'venue': VENUE,
                'city': CITY,
                'country_code': 'US',
                'description': block['description'],
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class CoastalSymphonyOfGeorgiaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='coastalsymphonyofgeorgia_org',
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
    CoastalSymphonyOfGeorgiaOrgCrawler().run()


if __name__ == '__main__':
    main()
