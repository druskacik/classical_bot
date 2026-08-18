import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cpmf.us/'
SEASON_URL = f'{SOURCE_URL}summerseason'
SOURCE = 'Cactus Pear Music Festival'

# The host rejects generic HTTP clients, but serves its static HTML to normal
# Chromium requests.  These headers reproduce the non-session-specific request
# made by the public site; no cookies or browser execution are required.
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'Upgrade-Insecure-Requests': '1',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s*'
    r'([A-Z]+\s+\d{1,2},\s+20\d{2})\s*[•|]\s*'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.I,
)
CITY_RE = re.compile(r'^([A-Za-z][A-Za-z .\'-]+),\s*TX$', re.I)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_card(card):
    title_node = card.select_one('.itemName')
    detail_node = card.select_one('.itemText')
    title = clean_text(title_node)
    detail = clean_text(detail_node)
    match = DATE_TIME_RE.search(detail)
    if not title or not match:
        return None

    try:
        event_date = datetime.strptime(
            match.group(1).title(), '%B %d, %Y'
        ).date().isoformat()
    except ValueError:
        return None

    time_from = None
    time_value = match.group(2).upper().replace(' ', '')
    for time_format in ('%I:%M%p', '%I%p'):
        try:
            time_from = datetime.strptime(time_value, time_format).strftime('%H:%M')
            break
        except ValueError:
            pass
    if time_from is None:
        return None

    lines = [line for line in detail.splitlines() if line]
    location = None
    location_index = None
    for index, line in enumerate(lines):
        city_match = CITY_RE.match(line)
        if city_match and index > 0:
            location = (lines[index - 1].strip(' ,'), city_match.group(1).strip())
            location_index = index
            break
    if not location or location_index is None:
        return None

    venue, city = location
    description_lines = lines[location_index + 1:]
    description = '\n'.join(description_lines).strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': SEASON_URL,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CpmfUsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cpmf_us',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SEASON_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Cactus Pear festival season',
                event='crawler_fetch_failed',
                level='error',
                url=SEASON_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for card in soup.select('.listText'):
            record = parse_card(card)
            if record:
                records.append(record)

        if not records:
            log_message(
                'No valid festival concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SEASON_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    CpmfUsCrawler().run()


if __name__ == '__main__':
    main()
