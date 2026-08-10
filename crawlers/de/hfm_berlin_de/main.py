import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfm-berlin.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'veranstaltungen/veranstaltungskalender/')
SOURCE = 'Hochschule für Musik Hanns Eisler Berlin'
CITY = 'Berlin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\\n', '\n').replace('\\,', ',').replace('\\;', ';')
    text = text.replace('\\\\', '\\').replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def form_data(form):
    return {
        field.get('name'): field.get('value', '')
        for field in form.select('input[name]')
    }


def listing_urls(session):
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = set()
    page = 1

    while True:
        form = soup.select_one('#calendarizeSearchForm')
        if not form:
            raise ValueError('Calendar search form was not found')

        data = form_data(form)
        data.update({
            'tx_calendarize_calendar[currentPage]': str(page),
            # The source retains a rolling archive. An intentionally early
            # lower bound discovers every archived event it still exposes.
            'tx_calendarize_calendar[startDate]': '2000-01-01',
            'tx_calendarize_calendar[endDate]': '2100-12-31',
        })
        action = urljoin(CALENDAR_URL, form.get('action') or CALENDAR_URL)
        response = session.post(action, data=data, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for link in soup.select('a[href*="/detail/event/"]'):
            href = link.get('href') or ''
            if '.ics' not in href:
                urls.add(urljoin(SOURCE_URL, href))

        if not soup.select_one('.c-pagebrowser__item--next'):
            break
        page += 1
        if page > 500:
            raise ValueError('Calendar pagination exceeded safety limit')

    return sorted(urls)


def unfold_ical(text):
    lines = []
    for line in text.replace('\r\n', '\n').split('\n'):
        if line.startswith((' ', '\t')) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def ical_fields(text):
    fields = {}
    for line in unfold_ical(text):
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        name = key.split(';', 1)[0]
        if name in {'SUMMARY', 'DESCRIPTION', 'LOCATION', 'DTSTART'}:
            fields[name] = (key, value)
    return fields


def parse_start(field):
    key, value = field
    value = value.strip()
    try:
        if 'VALUE=DATE' in key or re.fullmatch(r'\d{8}', value):
            parsed = datetime.strptime(value[:8], '%Y%m%d')
            return parsed.date().isoformat(), None

        is_utc = value.endswith('Z')
        parsed = datetime.strptime(value.rstrip('Z'), '%Y%m%dT%H%M%S')
        if is_utc:
            parsed = parsed.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Berlin'))
        else:
            parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Berlin'))
        return parsed.date().isoformat(), parsed.strftime('%H:%M')
    except ValueError:
        return None, None


def parse_ical(detail_url, text):
    fields = ical_fields(text)
    if not all(name in fields for name in ('SUMMARY', 'DTSTART', 'LOCATION')):
        return None

    title = clean_text(fields['SUMMARY'][1])
    venue = clean_text(fields['LOCATION'][1])
    event_date, time_from = parse_start(fields['DTSTART'])
    if not title or not venue or not event_date:
        return None
    try:
        date.fromisoformat(event_date)
    except ValueError:
        return None

    description_field = fields.get('DESCRIPTION')
    return {
        'title': title,
        'date': event_date,
        'url': detail_url,
        'time_from': time_from,
        'venue': venue,
        # This institutional calendar is for its Berlin events and named
        # Berlin partner halls. Do not derive a city from ticket/address text.
        'city': CITY,
        'country_code': 'DE',
        'description': clean_text(description_field[1]) if description_field else None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(session, detail_url):
    ical_url = detail_url.rstrip('/') + '.ics/'
    response = session.get(ical_url, timeout=45)
    response.raise_for_status()
    return parse_ical(detail_url, response.text)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_record, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class HfmBerlinDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfm_berlin_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
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
        return get_concerts()


def main():
    HfmBerlinDeCrawler().run()


if __name__ == '__main__':
    main()
