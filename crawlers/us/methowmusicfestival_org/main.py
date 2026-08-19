import re
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.methowmusicfestival.org/'
SOURCE = 'Methow Valley Chamber Music Festival'
ANNOUNCEMENTS_URL = urljoin(SOURCE_URL, 'announcements')
DEFAULT_VENUE = 'Methow Valley Community Center'
DEFAULT_CITY = 'Twisp'

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
        [
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ]
    )
    if name
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = unescape(text)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.search(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def make_date(year, month_name, day):
    try:
        return date(int(year), MONTHS[month_name.lower()], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def base_record(title, event_date, url, time_from, venue, city, description):
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_summer_festival(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('title')).split(' — ', 1)[0]
    year_match = re.search(r'\b(20\d{2})\b', title or page_url)
    if not year_match:
        return []
    year = year_match.group(1)

    records = []
    for item in soup.select('li.accordion-item'):
        heading = clean_text(item.select_one('.accordion-item__title'))
        match = re.search(r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})', heading, re.I)
        description = clean_text(item.select_one('.accordion-item__description'))
        if not match or not description:
            continue
        event_date = make_date(year, match.group(1), match.group(2))
        concert_match = re.search(r'(?:^|\n)CONCERT\s+([^\n]+)', description, re.I)
        time_from = parse_time(concert_match.group(1)) if concert_match else parse_time(description)
        record = base_record(
            f'{title} — {heading}', event_date, page_url, time_from,
            DEFAULT_VENUE, DEFAULT_CITY, description,
        )
        if record:
            records.append(record)
    return records


def publication_year(item):
    timestamp = item.get('publishOn')
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).year


def parse_announcement(item):
    title = clean_text(item.get('title', ''))
    body = clean_text(BeautifulSoup(item.get('body', ''), 'html.parser'))
    excerpt = clean_text(BeautifulSoup(item.get('excerpt', ''), 'html.parser'))
    text = '\n'.join(part for part in (excerpt, body) if part)
    url = urljoin(SOURCE_URL, item.get('fullUrl', ''))
    year = publication_year(item)
    records = []

    if title == 'Portland Cello Project: A Tribute to Stevie Wonder':
        match = re.search(
            r'Date:\s*(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),\s*(20\d{2})',
            text,
            re.I,
        )
        if match:
            event_date = make_date(match.group(3), match.group(1), match.group(2))
            time_match = re.search(r'Time:\s*([^\n]+)', text, re.I)
            records.append(base_record(
                title, event_date, url, parse_time(time_match.group(1)) if time_match else None,
                'Pipestone Canyon Ranch', 'Twisp', body,
            ))

    elif title == 'Cupid’s Quartet - Winter Concert' and year:
        dates = re.search(r'February\s+(\d{1,2})\s*&\s*(\d{1,2}),\s*(20\d{2})', text, re.I)
        if dates:
            for day, time_from in ((dates.group(1), '19:00'), (dates.group(2), '14:00')):
                records.append(base_record(
                    title, make_date(dates.group(3), 'February', day), url, time_from,
                    'The Merc Playhouse', DEFAULT_CITY, body,
                ))

    elif title == '“A Warm Winter Evening”':
        match = re.search(
            r'(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),\s*(20\d{2}).{0,40}?'
            r'at\s+((?:\d{1,2}:)?\d{1,2}\s*[AP]M)',
            text,
            re.I | re.S,
        )
        if match:
            records.append(base_record(
                title, make_date(match.group(3), match.group(1), match.group(2)), url,
                parse_time(match.group(4)), 'The Merc Playhouse', DEFAULT_CITY, body,
            ))

    elif title == 'Leavenworth Alphorns Kick-Off 30th Festival' and year:
        match = re.search(r'(' + '|'.join(MONTHS) + r')\s+(\d{1,2}).{0,20}?([\d: ]+[AP]M)', text, re.I)
        if match:
            records.append(base_record(
                title, make_date(year, match.group(1), match.group(2)), url,
                parse_time(match.group(3)), 'Twisp Town Park', DEFAULT_CITY, body,
            ))

    elif title == 'Enjoy Open Rehearsals Around the Valley' and year:
        locations = {
            'Mazama Community Club': 'Mazama',
            'Winthrop Library': 'Winthrop',
            'Motive Yoga': 'Winthrop',
            'Confluence Gallery': 'Twisp',
        }
        for venue, city in locations.items():
            match = re.search(
                re.escape(venue) + r'[^\n]*?(' + '|'.join(MONTHS) +
                r')\s+(\d{1,2})(?:st|nd|rd|th)?[^\n]*?([\d: ]+[ap]m)',
                text,
                re.I,
            )
            if match:
                records.append(base_record(
                    f'Open Rehearsal — {venue}',
                    make_date(year, match.group(1), match.group(2)), url,
                    parse_time(match.group(3)), venue, city, body,
                ))

    elif title == '2024 Summer Festival Programs & Artists':
        match = re.search(
            r'(June)\s+(\d{1,2})\s*&\s*(\d{1,2})\s+and\s+June\s+'
            r'(\d{1,2})\s*&\s*(\d{1,2})',
            text,
            re.I,
        )
        if match:
            for day in match.groups()[1:]:
                records.append(base_record(
                    '2024 Summer Festival', make_date(2024, 'June', day), url, None,
                    DEFAULT_VENUE, DEFAULT_CITY, body,
                ))

    return [record for record in records if record]


class MethowMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='methowmusicfestival_org',
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
        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            home_soup = BeautifulSoup(home_response.text, 'html.parser')
            festival_links = {
                urljoin(SOURCE_URL, link['href'])
                for link in home_soup.select('a[href]')
                if re.fullmatch(r'/20\d{2}-summer-festival/?', link.get('href', ''))
            }

            announcements_response = session.get(
                f'{ANNOUNCEMENTS_URL}?format=json', timeout=45
            )
            announcements_response.raise_for_status()
            announcements = announcements_response.json()

            records = []
            for page_url in sorted(festival_links):
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
                records.extend(parse_summer_festival(response.text, page_url))

            for item in announcements.get('items', []):
                records.extend(parse_announcement(item))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Methow Valley Chamber Music Festival events',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    MethowMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
