import base64
import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil.rrule import rrulestr

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.toledoopera.org/'
SOURCE = 'Toledo Opera'
CALENDAR_URL = f'{SOURCE_URL}resources/toledo-opera-events-calendar/'
CALENDAR_ID = 'c_eu31cvcjnkf18a3d1vr7dp4gmg@group.calendar.google.com'
ICAL_URL = (
    'https://calendar.google.com/calendar/ical/'
    'c_eu31cvcjnkf18a3d1vr7dp4gmg%40group.calendar.google.com/public/basic.ics'
)
TIMEZONE = ZoneInfo('America/New_York')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\\n', '\n').replace('\\,', ',')).strip()


def unfold_ical(text):
    return re.sub(r'\r?\n[ \t]', '', text)


def ical_value(block, name):
    match = re.search(rf'^{re.escape(name)}(?:;[^:]*)?:(.*)$', block, re.MULTILINE)
    return match.group(1).strip() if match else ''


def parse_ical_datetime(value):
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r'\d{8}', value):
        return datetime.strptime(value, '%Y%m%d').replace(tzinfo=TIMEZONE)
    if value.endswith('Z'):
        return datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(
            tzinfo=ZoneInfo('UTC')
        ).astimezone(TIMEZONE)
    return datetime.strptime(value, '%Y%m%dT%H%M%S').replace(tzinfo=TIMEZONE)


def event_url(uid):
    event_id = uid.removesuffix('@google.com')
    encoded = base64.b64encode(f'{event_id} {CALENDAR_ID}'.encode()).decode().rstrip('=')
    return f'https://www.google.com/calendar/event?eid={encoded}&ctz=America/New_York'


def venue_and_city(location):
    location = clean_text(location)
    if not location or location.upper() == 'TBA':
        return '', ''
    venue = location.split(',', 1)[0].strip()
    match = re.search(r',\s*([^,]+),\s*OH\s+\d{5}(?:-\d{4})?(?:,\s*USA)?$', location, re.I)
    city = clean_text(match.group(1)) if match else ''
    return venue, city


def recurrence_dates(block, start):
    rule = ical_value(block, 'RRULE')
    if not rule:
        return [start]
    try:
        values = list(rrulestr(rule, dtstart=start))
    except (ValueError, TypeError, OverflowError) as error:
        log_message(
            'Could not expand calendar recurrence',
            event='crawler_recurrence_error',
            level='warning',
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return [start]
    excluded = {
        parse_ical_datetime(value)
        for value in re.findall(r'^EXDATE(?:;[^:]*)?:(.*)$', block, re.MULTILINE)
    }
    return [value for value in values if value not in excluded]


def parse_calendar(text):
    records = []
    for block in unfold_ical(text).split('BEGIN:VEVENT')[1:]:
        if ical_value(block, 'STATUS').upper() == 'CANCELLED':
            continue
        title = clean_text(ical_value(block, 'SUMMARY'))
        start = parse_ical_datetime(ical_value(block, 'DTSTART'))
        venue, city = venue_and_city(ical_value(block, 'LOCATION'))
        uid = ical_value(block, 'UID')
        if not title or not start or not venue or not city or not uid:
            continue
        description = clean_text(ical_value(block, 'DESCRIPTION')) or None
        is_all_day = len(ical_value(block, 'DTSTART')) == 8
        for occurrence in recurrence_dates(block, start):
            records.append({
                'title': title,
                'date': occurrence.date().isoformat(),
                'url': event_url(uid),
                'time_from': None if is_all_day else occurrence.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def season_years(soup):
    text = clean_text(soup.get_text(' ', strip=True))
    match = re.search(r'(20\d{2})\s*[-–]\s*(?:20)?(\d{2,4})\s+Season', text, re.I)
    if not match:
        return None
    first = int(match.group(1))
    second = int(match.group(2))
    if second < 100:
        second += first // 100 * 100
    return first, second


def parse_season_page(soup, url, years):
    performance = soup.select_one('.performance')
    title_node = performance.select_one('h1') if performance else None
    date_node = performance.select_one('.date') if performance else None
    if not title_node or not date_node or not years:
        return []
    title = clean_text(title_node.get_text(' ', strip=True))
    date_text = clean_text(date_node.get_text(' ', strip=True))
    match = re.search(
        r'([A-Za-z]+)\s+(\d{1,2})\s+at\s+(\d{1,2}(?::\d{2})?\s*[ap]m)'
        r'(?:\s*&\s*(?:(\w+)\s+)?(\d{1,2})\s+at\s+'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m))?',
        date_text,
        re.I,
    )
    if not title or not match:
        return []
    month_one, day_one, time_one, month_two, day_two, time_two = match.groups()
    values = [(month_one, day_one, time_one)]
    if day_two:
        values.append((month_two or month_one, day_two, time_two))
    core = performance.find('div', class_='row')
    description = clean_text(core.get_text('\n', strip=True)) if core else ''
    description = description.replace(title, '', 1).replace(date_text, '', 1).strip() or None
    records = []
    for month, day, event_time in values:
        month_number = datetime.strptime(month, '%B').month
        year = years[0] if month_number >= 7 else years[1]
        try:
            event_date = datetime(year, month_number, int(day)).date().isoformat()
            parsed_time = datetime.strptime(event_time.upper().replace(' ', ''), '%I:%M%p')
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parsed_time.strftime('%H:%M'),
            'venue': 'Valentine Theatre',
            'city': 'Toledo',
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    calendar_response = session.get(ICAL_URL, timeout=45)
    calendar_response.raise_for_status()
    records = parse_calendar(calendar_response.text)

    home_response = session.get(SOURCE_URL, timeout=45)
    home_response.raise_for_status()
    home = BeautifulSoup(home_response.text, 'html.parser')
    years = season_years(home)
    links = {
        urljoin(SOURCE_URL, node.get('href'))
        for node in home.select('a[href*="upcoming-performances/season-event/"]')
    }
    for url in sorted(links):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_season_page(BeautifulSoup(response.text, 'html.parser'), url, years))
        except requests.RequestException as error:
            log_message(
                'Could not fetch season performance',
                event='crawler_detail_error',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['title'].casefold(), record['date'], record['time_from'], record['venue'].casefold())
        current = unique.get(key)
        if not current or record['url'].startswith(SOURCE_URL):
            unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No valid calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return result


class ToledoOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='toledoopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ToledoOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
