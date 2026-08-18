import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lmso.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
SOURCE = 'Lake Murray Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The orchestra currently uses these two recurring halls.  Wix exposes no
# structured location data on the concerts page, while the homepage confirms
# Harbison Theatre's city and postal address.
VENUE_CITIES = {
    'Harbison Theatre at Midlands Technical College': 'Irmo',
    'Harbison Theatre': 'Irmo',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u202f', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def visible_lines(html):
    soup = BeautifulSoup(html, 'html.parser')
    lines = []
    for value in soup.get_text('\n').splitlines():
        value = clean_text(value)
        if value and (not lines or value != lines[-1]):
            lines.append(value)
    return lines


def parse_date(value):
    value = re.sub(r'(\d)(?:st|nd|rd|th)', r'\1', clean_text(value), flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def listing_items(html):
    lines = visible_lines(html)
    records = []
    for index, value in enumerate(lines):
        if value != 'Title:' or index + 5 >= len(lines):
            continue
        if lines[index + 2] != 'Date:' or lines[index + 4] != 'Location:':
            continue
        records.append({
            'title': clean_text(lines[index + 1]),
            'date': parse_date(lines[index + 3]),
            'venue': clean_text(lines[index + 5]),
        })
    return records


def homepage_detail(html):
    lines = visible_lines(html)
    try:
        date_index = lines.index('Date')
        about_index = lines.index('About', date_index + 1)
        next_index = lines.index('Next Concert', about_index + 1)
    except ValueError:
        return {}

    datetime_text = lines[date_index + 1] if date_index + 1 < len(lines) else ''
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4}\s+at\s+'
        r'(\d{1,2}):?(\d{2})?\s*([AP]M)',
        datetime_text,
        flags=re.I,
    )
    if not match or next_index + 1 >= len(lines):
        return {}
    event_date = parse_date(datetime_text.split(' at ', 1)[0].split(', ', 1)[-1])
    # The preceding split drops an optional weekday but preserves month/date/year.
    if not event_date:
        date_match = re.search(r'[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4}', datetime_text)
        event_date = parse_date(date_match.group(0)) if date_match else None

    hour = int(match.group(2)) % 12
    if match.group(4).upper() == 'PM':
        hour += 12
    time_from = f'{hour:02d}:{int(match.group(3) or 0):02d}'

    description_parts = [
        line for line in lines[about_index + 1:next_index]
        if line not in {'About', 'Free Admission, donations graciously welcome.'}
    ]
    return {
        'title': clean_text(lines[next_index + 1]),
        'date': event_date,
        'time_from': time_from,
        'description': '\n\n'.join(dict.fromkeys(description_parts)) or None,
    }


def make_records(concerts_html, home_html):
    detail = homepage_detail(home_html)
    records = []
    for item in listing_items(concerts_html):
        title = item['title']
        venue = item['venue']
        city = VENUE_CITIES.get(venue)
        # Wix uses TBD/coming-soon cards for season placeholders.  They are not
        # concrete advertised performances under the project inclusion rules.
        if title.lower() in {'tbd', 'coming soon'} or not item['date'] or not city:
            continue

        is_featured = title == detail.get('title') and item['date'] == detail.get('date')
        records.append({
            'title': title,
            'date': item['date'],
            'url': SOURCE_URL if is_featured else CONCERTS_URL,
            'time_from': detail.get('time_from') if is_featured else None,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': detail.get('description') if is_featured else None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class LmsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lmso_org',
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
        responses = {}
        for url in (CONCERTS_URL, SOURCE_URL):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                responses[url] = response.text
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch LMSO page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                if url == CONCERTS_URL:
                    raise
                responses[url] = ''
        return sorted(
            make_records(responses[CONCERTS_URL], responses[SOURCE_URL]),
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    LmsoOrgCrawler().run()


if __name__ == '__main__':
    main()
