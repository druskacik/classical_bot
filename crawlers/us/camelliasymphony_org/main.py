import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.camelliasymphony.org/'
SOURCE = 'Camellia Symphony Orchestra'
PAGES_API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Referer': SOURCE_URL,
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*'
    r'(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:,\s*(20\d{2}))?\b',
    re.IGNORECASE,
)


def clean_text(value):
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value or '')
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_sections(html):
    soup = BeautifulSoup(html, 'html.parser')
    sections = []
    current = None
    for element in soup.find_all(['h2', 'p']):
        if element.name == 'h2':
            if current:
                sections.append(current)
            current = {'title': clean_text(element), 'parts': []}
        elif current:
            # Invalid legacy markup sometimes wraps later headings in a <p>.
            # Ignore those container paragraphs so adjacent concerts do not merge.
            if element.find('h2'):
                continue
            text = clean_text(element)
            if text and text not in current['parts']:
                current['parts'].append(text)
    if current:
        sections.append(current)
    return sections


def parse_date(text, default_year, first_month):
    match = DATE_RE.search(text)
    if not match:
        return None
    month = MONTHS[match.group(1).lower()]
    year = int(match.group(3)) if match.group(3) else default_year + (month < first_month)
    try:
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', text, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def parse_season_page(page):
    sections = event_sections(page.get('content', {}).get('rendered', ''))
    dated = []
    for section in sections:
        text = '\n'.join(section['parts'])
        match = DATE_RE.search(text)
        if match:
            dated.append((section, text, match))
    if not dated:
        return []

    published_year = int(page['date'][:4])
    first_month = MONTHS[dated[0][2].group(1).lower()]
    records = []
    for section, text, _ in dated:
        event_date = parse_date(text, published_year, first_month)
        if not event_date:
            continue
        date_text = next(part for part in section['parts'] if DATE_RE.search(part))
        venue = (
            'Benvenuti Performing Arts Center'
            if 'Benvenuti Performing Arts Center' in date_text
            else 'McClatchy High School'
        )
        records.append({
            'title': section['title'],
            'date': event_date,
            'url': page['link'],
            'time_from': parse_time(date_text) or '19:30',
            'venue': venue,
            'city': 'Sacramento',
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class CamelliaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='camelliasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            PAGES_API_URL,
            params={'per_page': 100, 'page': 1},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        pages = response.json()
        records = []
        for page in pages:
            if not page.get('slug', '').startswith('welcome-to-season'):
                continue
            try:
                records.extend(parse_season_page(page))
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Camellia Symphony season page',
                    event='crawler_item_failed',
                    level='warning',
                    url=page.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    CamelliaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
