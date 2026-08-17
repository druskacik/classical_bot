import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.basicallybeethoven.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts-festival'
SOURCE = 'Basically Beethoven'
CITY = 'Dallas'
VENUE = 'Central Commons'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = r'(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}'


def clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def fetch_html(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def make_record(title, event_date, url, description):
    return {
        'title': clean_text(title),
        'date': event_date,
        'url': url,
        'time_from': '15:00',
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_archive(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for title_node in soup.select('.info-element-title'):
        heading = clean_text(title_node.get_text(' ', strip=True))
        match = re.fullmatch(rf'({DATE_PATTERN})\s+-\s+(.+)', heading)
        if not match:
            continue
        title = clean_text(match.group(2))
        card_text = clean_text(title_node.parent.get_text(' ', strip=True))
        description = card_text[len(heading):].strip() or None
        event_date = parse_date(match.group(1))
        if title and event_date:
            records.append(make_record(title, event_date, SOURCE_URL, description))
    return records


def parse_upcoming(text):
    marker = 'Basically Beethoven Hallam Concerts 2026 - 2027'
    start = text.find(marker)
    if start < 0:
        return []
    section = text[start:start + 1200]
    dates = []
    # Wix groups several month/day values under one trailing year.
    for year_match in re.finditer(r'((?:(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2},?\s*)+)(20\d{2})', section):
        year = year_match.group(2)
        for month_day in re.findall(r'(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})', year_match.group(1)):
            event_date = parse_date(f'{month_day[0]} {month_day[1]}, {year}')
            if event_date:
                dates.append(event_date)
    description = (
        'Free classical chamber music concert in the Basically Beethoven Hallam Concert series. '
        'The site states that concerts are on Saturdays at 3pm at Central Commons.'
    )
    records = []
    for event_date in dates:
        title = 'Basically Beethoven Hallam Concert'
        if event_date == '2026-10-24':
            title = 'Catharine Lysinger — Of Many Lands and Peoples'
        records.append(make_record(title, event_date, CONCERTS_URL, description))
    return records


class BasicallyBeethovenOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='basicallybeethoven_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = parse_archive(fetch_html(session, SOURCE_URL))
        concerts_html = fetch_html(session, CONCERTS_URL)
        concerts_text = clean_text(BeautifulSoup(concerts_html, 'html.parser').get_text(' ', strip=True))
        records.extend(parse_upcoming(concerts_text))
        records = sorted(records, key=lambda item: (item['date'], item['title']))
        if not records:
            log_message(
                'No concert occurrences found',
                event='crawler_empty_listing',
                level='warning',
                url=CONCERTS_URL,
                record_count=0,
            )
        return records


def main():
    BasicallyBeethovenOrgCrawler().run()


if __name__ == '__main__':
    main()
