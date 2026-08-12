import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.exonsingers.org.uk/'
SOURCE = 'The Exon Singers'
FESTIVAL_URLS = (
    f'{SOURCE_URL}festival',
    f'{SOURCE_URL}copy-of-festival',
)
CITY = 'Tavistock'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
EVENT_RE = re.compile(
    rf'^(?P<title>.+?)\s+'
    rf'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    rf'(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{MONTHS})'
    rf'(?:\s+(?P<year>\d{{4}}))?\s+'
    rf'(?P<hour>\d{{1,2}})(?:[.:](?P<minute>\d{{2}}))?\s*(?P<ampm>am|pm)\b',
    re.IGNORECASE | re.DOTALL,
)
VENUE_RE = re.compile(
    r'\b(?P<venue>(?:The Great Barn,\s*)?National Trust Buckland Abbey(?: Great Barn)?|'
    r'Tavistock Parish Church)\b',
    re.IGNORECASE,
)


def clean_text(node):
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalise_venue(value):
    if 'Buckland Abbey' in value:
        return 'The Great Barn, Buckland Abbey'
    return 'St Eustachius Parish Church'


def parse_block(text, page_url, page_year):
    flat_text = re.sub(r'\s+', ' ', text).strip()
    match = EVENT_RE.match(flat_text)
    if not match:
        return None

    supplied_year = int(match.group('year')) if match.group('year') else None
    # The archived page contains one obvious "2924" typo. Use a plausible
    # explicit occurrence year, otherwise the dominant year on the programme.
    year = supplied_year if supplied_year and 2000 <= supplied_year <= 2100 else page_year
    try:
        event_date = datetime.strptime(
            f"{match.group('day')} {match.group('month')} {year}", '%d %B %Y'
        ).date().isoformat()
    except ValueError:
        return None

    venue_match = VENUE_RE.search(flat_text, match.end())
    if not venue_match:
        return None

    hour = int(match.group('hour'))
    minute = int(match.group('minute') or 0)
    if hour not in range(1, 13) or minute > 59:
        return None
    if match.group('ampm').lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group('ampm').lower() == 'am' and hour == 12:
        hour = 0

    title = re.sub(r'\s+', ' ', match.group('title')).strip(' :')
    description = flat_text[venue_match.end():].strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': page_url,
        'time_from': f'{hour:02d}:{minute:02d}',
        'venue': normalise_venue(venue_match.group('venue')),
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_festival_page(content, page_url):
    soup = BeautifulSoup(content, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []
    blocks = [
        clean_text(node)
        for node in main.select('.wixui-rich-text, .wixui-collapsible-text')
    ]
    explicit_years = []
    for block in blocks:
        match = EVENT_RE.match(re.sub(r'\s+', ' ', block))
        if match and match.group('year'):
            year = int(match.group('year'))
            if 2000 <= year <= 2100:
                explicit_years.append(year)
    if not explicit_years:
        return []
    page_year = max(set(explicit_years), key=explicit_years.count)

    records = []
    for block in blocks:
        record = parse_block(block, page_url, page_year)
        if record:
            records.append(record)
            continue
        # Wix can split one programme across several adjacent rich-text nodes.
        if records and block and not block.lower().startswith('festival '):
            records[-1]['description'] = '\n\n'.join(
                part for part in (records[-1]['description'], block) if part
            ) or None
    return records


class ExonSingersCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='exonsingers_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        records = []
        for url in FESTIVAL_URLS:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_festival_page(response.content, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Exon Singers festival page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    ExonSingersCrawler().run()


if __name__ == '__main__':
    main()
