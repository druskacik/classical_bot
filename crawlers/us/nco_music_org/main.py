import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nco-music.org/'
SOURCE = 'Nashua Chamber Orchestra'
ARCHIVE_URL = urljoin(SOURCE_URL, '20252026')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'(?P<month1>{MONTHS})\s+(?P<day1>\d{{1,2}})'
    rf'(?:\s*(?:&|and)\s*(?:(?P<month2>{MONTHS})\s+)?(?P<day2>\d{{1,2}}))?'
    rf',?\s*(?P<year>\d{{4}})',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'^(?:19|20)\d{2}(?:-(?:19|20)?\d{2})?$')
NON_EVENT_TITLE_RE = re.compile(r'\b(?:gala|silent auction|fundraiser)\b', re.IGNORECASE)

VENUES_BY_WEEKDAY = {
    5: ('Judd Gregg Hall Auditorium, Nashua Community College', 'Nashua'),
    6: ('Milford Town Hall Auditorium', 'Milford'),
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_dates(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return []

    values = [(match['month1'], match['day1'])]
    if match['day2']:
        values.append((match['month2'] or match['month1'], match['day2']))

    dates = []
    for month, day in values:
        try:
            dates.append(
                datetime.strptime(
                    f'{month} {day} {match["year"]}', '%B %d %Y'
                ).date()
            )
        except ValueError:
            continue
    return dates


def archive_urls(soup):
    urls = {ARCHIVE_URL}
    for link in soup.select('main nav a[href]'):
        if SEASON_RE.fullmatch(clean_text(link.get_text(' ', strip=True))):
            urls.add(urljoin(SOURCE_URL, link['href']))
    return sorted(urls, reverse=True)


def page_events(soup):
    main = soup.select_one('main')
    if not main:
        return []

    nodes = main.find_all(['h1', 'h2', 'h3', 'h4', 'p'])
    event_markers = []
    for index, node in enumerate(nodes):
        date_text = clean_text(node.get_text(' ', strip=True))
        dates = parse_dates(date_text)
        if not dates:
            continue

        title_index = None
        for candidate_index in range(index - 1, -1, -1):
            if nodes[candidate_index].name.startswith('h'):
                title_index = candidate_index
                break
        if title_index is not None:
            event_markers.append((title_index, index, dates))

    events = []
    for marker_index, (title_index, date_index, dates) in enumerate(event_markers):
        title = clean_text(nodes[title_index].get_text(' ', strip=True))
        if not title or NON_EVENT_TITLE_RE.search(title):
            continue

        end_index = (
            event_markers[marker_index + 1][0]
            if marker_index + 1 < len(event_markers)
            else len(nodes)
        )
        description_parts = []
        for node in nodes[date_index + 1:end_index]:
            text = clean_text(node.get_text(' ', strip=True))
            if text and text not in description_parts:
                description_parts.append(text)
        events.append((title, dates, '\n'.join(description_parts) or None))
    return events


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(ARCHIVE_URL, timeout=45)
    response.raise_for_status()
    urls = archive_urls(BeautifulSoup(response.text, 'html.parser'))

    records = []
    for url in urls:
        try:
            if url == ARCHIVE_URL:
                page_response = response
            else:
                page_response = session.get(url, timeout=45)
                page_response.raise_for_status()
            soup = BeautifulSoup(page_response.text, 'html.parser')
        except requests.RequestException as error:
            log_message(
                'Season page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        for title, dates, description in page_events(soup):
            for event_date in dates:
                location = VENUES_BY_WEEKDAY.get(event_date.weekday())
                if not location:
                    continue
                venue, city = location
                records.append({
                    'title': title,
                    'date': event_date.isoformat(),
                    'url': url,
                    'time_from': None,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

    if not records:
        log_message(
            'No dated concerts found in season archive',
            event='crawler_empty_listing',
            level='warning',
            url=ARCHIVE_URL,
            record_count=0,
        )

    unique_records = {
        (record['title'], record['date'], record['venue']): record
        for record in records
    }
    return sorted(
        unique_records.values(),
        key=lambda item: (item['date'], item['title'], item['venue']),
    )


class NcoMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nco_music_org',
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
    NcoMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
