import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brocktonsymphony.org/'
CURRENT_SEASON_URL = urljoin(SOURCE_URL, 'season_current.php')
ARCHIVE_INDEX_URL = urljoin(SOURCE_URL, 'season_2024.php')
SOURCE = 'Brockton Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Christ Congregational Church': 'Brockton',
    'Christ Congregational Church U.C.C.': 'Brockton',
    'Buckley Performing Arts Center': 'Brockton',
    'Massasoit Community College, Buckley Performing Arts Center': 'Brockton',
    'Oliver Ames High School': 'North Easton',
    'Oliver Ames High School Auditorium': 'North Easton',
    'Stoughton High School': 'Stoughton',
    'First Church': 'Cambridge',
    'Brockton Public Library': 'Brockton',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|October|November|December'
)
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,|\s)+(20\d{{2}})',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value, season_start=None):
    value = clean_text(value)
    match = DATE_RE.search(value)
    if match:
        month, day, year = match.groups()
    else:
        match = re.search(
            rf'({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b', value, re.I
        )
        if not match or season_start is None:
            return None
        month, day = match.groups()
        month_number = datetime.strptime(month[:3], '%b').month
        year = str(season_start + (month_number < 7))
    try:
        return datetime.strptime(f'{month[:3]} {day} {year}', '%b %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_times(value):
    times = []
    for hour, minute, meridiem in TIME_RE.findall(clean_text(value)):
        hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
        parsed = f'{hour:02d}:{int(minute or 0):02d}'
        if parsed not in times:
            times.append(parsed)
    return times or [None]


def parse_location(value):
    value = clean_text(value)
    value = re.sub(r'\b\d{1,5}\s+[A-Za-z0-9 .\'-]+(?:St\.?|Street|Ave\.?|Avenue|Rd\.?|Road)\b.*$', '', value, flags=re.I).strip(' ,')
    for venue, city in VENUE_CITIES.items():
        if venue.casefold() in value.casefold():
            return venue, city
    match = re.match(r'(.+?),\s*([A-Za-z][A-Za-z .\'-]+)(?:,\s*MA)?$', value)
    if match:
        venue, city = (clean_text(part) for part in match.groups())
        if venue and city:
            return venue, city
    return '', ''


def season_start_year(soup, url):
    match = re.search(r'(20\d{2})\s*[-–‑]\s*20\d{2}', soup.get_text(' ', strip=True))
    if match:
        return int(match.group(1))
    match = re.search(r'season_(20\d{2})\.php', url)
    return int(match.group(1)) if match else None


def description_for(title_node, next_title):
    parts = []
    for node in title_node.find_all_next('p'):
        if node is next_title:
            break
        classes = set(node.get('class') or [])
        if classes & {'concert', 'date', 'location'}:
            continue
        text = clean_text(node.get_text('\n', strip=True))
        if not text or re.search(r'(?:order|purchase).*tickets?', text, re.I):
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_season(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('#content')
    if not content:
        return []
    titles = content.select('p.concert')
    start_year = season_start_year(soup, url)
    records = []
    for index, title_node in enumerate(titles):
        next_title = titles[index + 1] if index + 1 < len(titles) else None
        title = clean_text(title_node.get_text(' ', strip=True))
        description = description_for(title_node, next_title)
        for date_node in title_node.find_all_next('p', class_='date'):
            if next_title and next_title in date_node.find_all_previous('p', class_='concert', limit=1):
                break
            previous_title = date_node.find_previous('p', class_='concert')
            if previous_title is not title_node:
                break
            event_date = parse_date(date_node.get_text(' ', strip=True), start_year)
            location_node = date_node.find_next('p', class_='location')
            if not event_date or not location_node:
                continue
            following_title = location_node.find_previous('p', class_='concert')
            if following_title is not title_node:
                continue
            venue, city = parse_location(location_node.get_text(' ', strip=True))
            if not title or not venue or not city:
                continue
            for time_from in parse_times(date_node.get_text(' ', strip=True)):
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
    return records


def season_urls(session):
    response = session.get(ARCHIVE_INDEX_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = [CURRENT_SEASON_URL, ARCHIVE_INDEX_URL]
    for link in soup.select('a[href]'):
        href = urljoin(ARCHIVE_INDEX_URL, link.get('href'))
        if re.fullmatch(r'https://www\.brocktonsymphony\.org/season_20\d{2}\.php', href):
            urls.append(href)
    return list(dict.fromkeys(urls))


class BrocktonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brocktonsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url in season_urls(session):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_season(response.text, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Brockton Symphony season page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        unique = {
            (item['title'], item['date'], item['time_from'], item['venue'], item['city']): item
            for item in records
        }
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    BrocktonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
