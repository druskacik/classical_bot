import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://glaciersymphony.org/'
SOURCE = 'Glacier Symphony'
LISTING_URLS = [urljoin(SOURCE_URL, 'concerts'), urljoin(SOURCE_URL, 'concerts-1')]
DEFAULT_CITY = 'Kalispell'
DEFAULT_VENUE = 'McClaren Hall at Wachholz College Center'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ['January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'],
        start=1,
    )
}
MONTH_PATTERN = '|'.join(MONTHS)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)).strip()


def season_years(soup):
    text = clean_text(soup)
    match = re.search(r'(20\d{2})\s*[-–]\s*(20\d{2})', text)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def parse_date_heading(value, start_year, end_year, forced_year=None):
    text = clean_text(value)
    match = re.search(
        rf'\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?'
        rf'(?:\s*(?:-|–|and|\+)\s*(?:(?:{MONTH_PATTERN})\.?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?)?'
        r'(?:\s*,?\s*(20\d{2}))?',
        text,
        re.I,
    )
    if not match:
        return []
    month = MONTHS[match.group(1).lower()]
    days = [int(match.group(2))]
    if match.group(3):
        days.append(int(match.group(3)))
    explicit_year = int(match.group(4)) if match.group(4) else None
    year = explicit_year or forced_year or (start_year if month >= 7 else end_year)
    if not year:
        return []
    dates = []
    for day in days:
        try:
            dates.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            continue
    return dates


def parse_times(value):
    times = []
    for hour, minute, meridiem in re.findall(
        r'\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b', value, re.I
    ):
        hour = int(hour)
        minute = int(minute or 0)
        if meridiem.lower().startswith('p') and hour != 12:
            hour += 12
        elif meridiem.lower().startswith('a') and hour == 12:
            hour = 0
        candidate = f'{hour:02d}:{minute:02d}'
        if candidate not in times:
            times.append(candidate)
    return times


def venue_from_heading(value):
    text = clean_text(value)
    parts = re.split(r'\s+[–—]\s+', text, maxsplit=1)
    if len(parts) > 1:
        venue = re.sub(r'\s+', ' ', parts[1]).strip(' .')
        if venue:
            if venue.lower() == 'mcclaren hall':
                return DEFAULT_VENUE
            return venue
    return DEFAULT_VENUE


def event_url(block, listing_url):
    section = block.find_parent('section') or block.parent
    links = section.select('a[href]')
    preferred = next(
        (link for link in links if re.search(r'buy|ticket', clean_text(link), re.I)),
        None,
    )
    href = (preferred or (links[0] if links else None))
    return urljoin(listing_url, href.get('href')) if href and href.get('href') else listing_url


def records_from_page(html, listing_url):
    soup = BeautifulSoup(html, 'html.parser')
    start_year, end_year = season_years(soup)
    records = []
    past_cutoff = None
    past_heading = next(
        (node for node in soup.find_all(['h1', 'h2']) if clean_text(node).lower() == 'past concerts'),
        None,
    )
    for block in soup.select('.sqs-html-content'):
        title_node = block.find(['h2', 'h3'])
        date_node = next(
            (node for node in block.find_all('h4') if re.search(rf'\b(?:{MONTH_PATTERN})\b', clean_text(node), re.I)),
            None,
        )
        if not title_node or not date_node:
            continue
        title = clean_text(title_node)
        is_old_archive = past_heading is not None and past_heading.find_next('div', class_='sqs-html-content') is not None \
            and past_heading.find_next('div', class_='sqs-html-content') is block
        if past_cutoff is not None:
            is_old_archive = True
        forced_year = None
        if is_old_archive:
            date_match = re.search(rf'\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})', clean_text(date_node), re.I)
            if date_match:
                month = MONTHS[date_match.group(1).lower()]
                day = int(date_match.group(2))
                candidate_year = past_cutoff.year if past_cutoff else start_year
                candidate = datetime(candidate_year, month, day).date()
                if past_cutoff and candidate > past_cutoff:
                    candidate_year -= 1
                    candidate = datetime(candidate_year, month, day).date()
                forced_year = candidate_year
                past_cutoff = candidate
        dates = parse_date_heading(date_node, start_year, end_year, forced_year)
        if not title or not dates:
            continue
        paragraphs = [clean_text(node) for node in block.find_all('p')]
        paragraphs = [text for text in paragraphs if text]
        times = parse_times(' '.join(paragraphs[:3]))
        description = '\n\n'.join(paragraphs) or None
        venue = venue_from_heading(date_node)
        if 'hilton garden ballroom' in ' '.join(paragraphs).lower():
            venue = 'Hilton Garden Ballroom'
        url = event_url(block, listing_url)

        if len(dates) == 1:
            event_times = times or [None]
            pairs = [(dates[0], time) for time in event_times]
        elif len(times) == len(dates):
            pairs = list(zip(dates, times))
        elif len(dates) == 2 and len(times) == 3:
            pairs = [(dates[0], times[0]), (dates[0], times[1]), (dates[1], times[2])]
        else:
            pairs = [(date, None) for date in dates]

        for event_date, time_from in pairs:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': DEFAULT_CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class GlacierSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='glaciersymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for listing_url in LISTING_URLS:
            try:
                response = session.get(listing_url, timeout=45)
                response.raise_for_status()
                page_records = records_from_page(response.text, listing_url)
                records.extend(page_records)
                log_message(
                    'Concert listing scraped',
                    event='crawler_listing_scraped',
                    url=listing_url,
                    record_count=len(page_records),
                )
            except requests.RequestException as error:
                log_message(
                    'Concert listing request failed',
                    event='crawler_listing_failed',
                    level='warning',
                    url=listing_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        if not records:
            log_message(
                'No concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    GlacierSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
