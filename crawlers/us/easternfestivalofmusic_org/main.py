import csv
import io
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.easternfestivalofmusic.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Eastern Festival of Music'
EVENTS_CSV_URL = (
    'https://docs.google.com/spreadsheets/d/'
    '1zjhmDd9mhYNryry7oEd6yht0rar-QMS54yv6I_V8n-s/gviz/tq'
    '?tqx=out:csv&sheet=EventGridToWebsite'
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/csv,text/plain;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    for pattern in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p', '%H:%M'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_location(value):
    lines = [line.strip() for line in clean_text(value).splitlines() if line.strip()]
    if not lines:
        return '', ''

    venue = re.sub(r'\s+-\s+Free Event\s*$', '', lines[0], flags=re.I).strip()
    city = ''
    for line in lines[1:]:
        match = re.search(r'([^,\n]+),\s*NC\b', line, re.I)
        if match:
            city = match.group(1).strip()
            break

    # Both venues in the published festival calendar are on the Guilford
    # College campus in Greensboro. The sheet omits the city for Carnegie Room.
    if not city and venue in {'Carnegie Room (in Hege Library)', 'Carnegie Room (Hege Library)'}:
        city = 'Greensboro'
    return venue, city


def event_url(value):
    value = (value or '').strip()
    if value.startswith(('http://', 'https://')):
        return value
    return f'{EVENTS_URL}#list'


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_CSV_URL, timeout=45)
    response.raise_for_status()

    records = []
    for row in csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))):
        if clean_text(row.get('ShowEvent')).lower() not in {'yes', 'true', '1'}:
            continue

        title = clean_text(row.get('Title'))
        event_date = parse_date(row.get('StartDate'))
        venue, city = parse_location(row.get('Address'))
        if not title or not event_date or not venue or not city:
            log_message(
                'Skipping event with incomplete required fields',
                event='crawler_record_skipped',
                level='warning',
                url=EVENTS_URL,
                has_title=bool(title),
                has_date=bool(event_date),
                has_venue=bool(venue),
                has_city=bool(city),
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': event_url(row.get('Link')),
            'time_from': parse_time(row.get('Time')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': clean_text(row.get('Description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No published concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_CSV_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class EasternFestivalOfMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='easternfestivalofmusic_org',
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
    EasternFestivalOfMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
