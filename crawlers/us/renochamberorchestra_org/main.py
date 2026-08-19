import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.renochamberorchestra.org/'
LISTING_URL = f'{SOURCE_URL}tickets'
SOURCE = 'Reno Chamber Orchestra'
CITY = 'Reno'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(\d{1,2})(?:\s*&\s*(\d{1,2}))?(?:,?\s*(20\d{2}))?\b',
    re.IGNORECASE,
)
VENUE_RE = re.compile(r'\b(?:Nightingale Concert Hall|Hall Recital Hall)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_blocks(soup):
    blocks = []
    seen = set()
    for link in soup.find_all('a', href=True):
        label = clean_text(link.get_text(' ', strip=True))
        if 'BUY' not in label.upper() or 'TICKET' not in label.upper():
            continue
        if 'SUBSCRIPTION' in label.upper():
            continue
        block = link.find_parent('div', class_='wH18kY')
        if block is not None and id(block) not in seen:
            seen.add(id(block))
            blocks.append((block, link['href']))
    return blocks


def parse_blocks(blocks):
    parsed = []
    previous_year = None
    previous_month = None

    for block, url in blocks:
        lines = [clean_text(line) for line in block.get_text('\n', strip=True).splitlines()]
        lines = [line for line in lines if line]
        date_index = next((i for i, line in enumerate(lines) if DATE_RE.search(line)), None)
        if date_index is None:
            continue

        match = DATE_RE.search(lines[date_index])
        month_name, first_day, second_day, explicit_year = match.groups()
        month = datetime.strptime(month_name[:3], '%b').month
        year = int(explicit_year) if explicit_year else previous_year
        if year is None:
            continue
        if not explicit_year and previous_month is not None and month < previous_month:
            year += 1

        title = next((line for line in lines[:date_index] if line), '')
        venue = next((VENUE_RE.search(line).group(0) for line in lines if VENUE_RE.search(line)), '')
        if not title or not venue:
            continue

        excluded = {title, lines[date_index], venue}
        description_lines = [
            line for line in lines
            if line not in excluded and not ('BUY' in line.upper() and 'TICKET' in line.upper())
        ]
        description = '\n'.join(dict.fromkeys(description_lines)) or None

        for day in (first_day, second_day):
            if not day:
                continue
            try:
                event_date = datetime(year, month, int(day)).date().isoformat()
            except ValueError:
                continue
            parsed.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        previous_year = year
        previous_month = month

    return parsed


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(LISTING_URL, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Concert listing request failed',
            event='crawler_request_failed',
            level='error',
            url=LISTING_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = parse_blocks(event_blocks(BeautifulSoup(response.text, 'html.parser')))
    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class RenoChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='renochamberorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    RenoChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
