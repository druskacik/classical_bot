import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wellesleysymphony.org/'
SOURCE = 'Wellesley Symphony'
CURRENT_SEASON_URL = urljoin(SOURCE_URL, 'concerts')
ARCHIVE_URLS = (
    (urljoin(SOURCE_URL, 'copy-of-2024-2025-season'), '2025-2026'),
    (urljoin(SOURCE_URL, '2024-2025-season'), '2024-2025'),
)
VENUE = 'MassBay Community College Auditorium'
ARCHIVE_VENUE = 'MassBay Community College'
CITY = 'Wellesley'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_text(soup):
    main = soup.find('main') or soup.body or soup
    return clean_text(main)


def parse_date(value, season=None):
    value = clean_text(value)
    match = re.search(
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+'
        r'(\d{1,2})(?:,)?(?:\s+(\d{4}))?',
        value,
        re.I,
    )
    if not match:
        return ''
    year = match.group(3)
    if not year and season:
        start_year, end_year = map(int, season.split('-'))
        month = datetime.strptime(match.group(1)[:3], '%b').month
        year = str(start_year if month >= 7 else end_year)
    if not year:
        return ''
    try:
        return datetime.strptime(
            f'{match.group(1)[:3]} {match.group(2)} {year}', '%b %d %Y'
        ).date().isoformat()
    except ValueError:
        return ''


def detail_record(url, soup):
    text = page_text(soup)
    heading = soup.find('h1')
    title = clean_text(heading) if heading else clean_text(soup.title).split('|', 1)[0]
    date_match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
        r'[A-Z][a-z]+\s+\d{1,2},\s+\d{4}',
        text,
    )
    event_date = parse_date(date_match.group(0)) if date_match else ''
    time_match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', text, re.I)
    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12 + (12 if time_match.group(3).lower() == 'p' else 0)
        time_from = f'{hour:02d}:{int(time_match.group(2) or 0):02d}'

    description = None
    venue_marker = re.search(r'MassBay Community College Auditorium[^\n]*', text, re.I)
    if venue_marker:
        body = text[venue_marker.end():]
        body = re.split(r'Follow us on|Voice Mail:', body, maxsplit=1, flags=re.I)[0]
        description = clean_text(body) or None

    if not all((title, event_date, url)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


DATE_HEADING = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*'
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+'
    r'\d{1,2}(?:,)?(?:\s+\d{4})?',
    re.I,
)


def archive_records(url, season, soup):
    text = page_text(soup)
    marker = re.search(re.escape(season) + r'\s+Season', text, re.I)
    if marker:
        text = text[marker.end():]
    text = re.split(r'Follow us on|Voice Mail:', text, maxsplit=1, flags=re.I)[0]
    matches = list(DATE_HEADING.finditer(text))
    records = []
    for index, match in enumerate(matches):
        segment = clean_text(text[match.start():matches[index + 1].start() if index + 1 < len(matches) else None])
        lines = [line.strip(' ,–-') for line in segment.splitlines() if line.strip(' ,–-')]
        if not lines:
            continue
        heading = lines[0]
        title = re.sub(DATE_HEADING, '', heading, count=1).strip(' ,–-')
        title = re.sub(r'^\d{1,2}(?::\d{2})?\s*[ap]m\s*', '', title, flags=re.I).strip(' ,–-')
        if not title and len(lines) > 1:
            title = lines[1]
        event_date = parse_date(match.group(0), season)
        time_match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*pm\b', segment, re.I)
        time_from = None
        if time_match:
            time_from = f'{int(time_match.group(1)) % 12 + 12:02d}:{int(time_match.group(2) or 0):02d}'
        description = '\n'.join(lines[1:]).strip() or None
        if not title or not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': ARCHIVE_VENUE,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class WellesleySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wellesleysymphony_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CURRENT_SEASON_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        detail_urls = sorted({
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('a[href*="/concerts/"]')
            if re.search(r'/concerts/\d{4}/[^/]+$', link.get('href', ''))
        })

        records = []
        for url in detail_urls:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = detail_record(url, BeautifulSoup(detail_response.text, 'html.parser'))
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Wellesley Symphony concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title or date is missing',
                )

        for archive_url, season in ARCHIVE_URLS:
            archive_response = session.get(archive_url, timeout=45)
            archive_response.raise_for_status()
            records.extend(archive_records(
                archive_url,
                season,
                BeautifulSoup(archive_response.text, 'html.parser'),
            ))

        if not records:
            log_message(
                'No Wellesley Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=CURRENT_SEASON_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    WellesleySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
