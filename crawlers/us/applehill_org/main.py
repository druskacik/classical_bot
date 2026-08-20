import re
from datetime import date

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://applehill.org/'
SOURCE = 'Apple Hill Center for Chamber Music'
PROGRAM_URL = 'https://applehill.org/concerts/concerts-apple-hill/'
API_URL = 'https://applehill.org/wp-json/wp/v2/pages/1729'
VENUE = 'Louise Shonk Kelly Concert Barn'
CITY = 'Nelson'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

EVENT_HEADING_RE = re.compile(
    r'\bConcert\s+[A-Z0-9]+\s*[–—-]\s*'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(20\d{2})\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    if isinstance(value, Tag):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(match):
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\bEvent begins at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', text, re.I)
    if not match:
        return '19:30'
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def event_sections(content_html):
    soup = BeautifulSoup(content_html, 'html.parser')
    sections = []
    current = []
    for node in list(soup.children):
        if isinstance(node, Tag) and node.name == 'hr':
            if current:
                sections.append(current)
            current = []
        else:
            current.append(node)
    if current:
        sections.append(current)
    return sections


def parse_section(nodes):
    text = clean_text('\n'.join(str(node) for node in nodes))
    match = EVENT_HEADING_RE.search(text)
    if not match:
        return None
    event_date = parse_date(match)
    if not event_date:
        return None

    heading_line = next(
        (line.strip() for line in text.splitlines() if EVENT_HEADING_RE.search(line)),
        '',
    )
    title = re.sub(r'\s*[–—-]\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),.*$', '', heading_line)
    title = re.sub(r'^.*?(Concert\s+[A-Z0-9]+)$', r'\1', title, flags=re.I).strip()
    if not title:
        return None

    # The site publishes all occurrences on this single canonical programme
    # page. Preserve the full section so programme works remain available to
    # the downstream programme extractor.
    return {
        'title': title,
        'date': event_date,
        'url': PROGRAM_URL,
        'time_from': parse_time(text),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ApplehillOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='applehill_org',
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
        try:
            response = requests.get(API_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Apple Hill concert page',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        content = (payload.get('content') or {}).get('rendered') or ''
        records = [parse_section(section) for section in event_sections(content)]
        records = [record for record in records if record]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ApplehillOrgCrawler().run()


if __name__ == '__main__':
    main()
