import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bvso.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts/')
SOURCE = 'Brazos Valley Symphony Orchestra'
CITY = 'College Station'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_years(soup):
    match = re.search(r'\b(20\d{2})\s*[-–]\s*(20\d{2}|\d{2})\b', clean_text(soup))
    if not match:
        return None
    first = int(match.group(1))
    second_value = match.group(2)
    second = int(second_value) if len(second_value) == 4 else first // 100 * 100 + int(second_value)
    return first, second


def parse_time(value):
    match = re.search(r'Concert Starts:\s*(\d{1,2}(?::\d{2})?\s*[AP]M)', value, re.I)
    if not match:
        return None
    normalized = re.sub(r'\s+', ' ', match.group(1).upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_venue(value):
    location = re.search(
        r'Concert Starts:\s*\d{1,2}(?::\d{2})?\s*[AP]M\s*\n+([^\n]+)',
        value,
        re.I,
    )
    if location:
        venue = clean_text(location.group(1))
        if venue and not re.search(r'^(featuring|program)\b', venue, re.I):
            return venue
    return None


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    listing = fetch_soup(session, LISTING_URL)
    years = season_years(listing)
    if not years:
        log_message(
            'Concert season years not found',
            event='crawler_parse_warning',
            level='warning',
            url=LISTING_URL,
            error_type='MissingSeasonYears',
        )
        return []

    records = []
    for card in listing.select('.mkdf-event-list-item'):
        title = clean_text(card.select_one('.mkdf-eli-title'))
        day = clean_text(card.select_one('.mkdf-el-date-separated h1'))
        month_name = clean_text(card.select_one('.mkdf-el-date-separated h6')).lower()
        link = card.select_one('a[href*="/show-item/"]')
        if not title or not day.isdigit() or month_name not in MONTHS or not link:
            continue

        month = MONTHS[month_name]
        year = years[0] if month >= 9 else years[1]
        try:
            event_date = datetime(year, month, int(day)).date().isoformat()
        except ValueError:
            continue

        url = urljoin(LISTING_URL, link.get('href'))
        try:
            detail = fetch_soup(session, url)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        content = detail.select_one('.mkdf-single-show-main-content')
        description = clean_text(content)
        venue = parse_venue(description)
        if not venue:
            log_message(
                'Concert venue not found',
                event='crawler_record_skipped',
                level='warning',
                url=url,
                error_type='MissingVenue',
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(description),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BvsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bvso_org',
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
        return scrape_concerts()


def main():
    BvsoOrgCrawler().run()


if __name__ == '__main__':
    main()
