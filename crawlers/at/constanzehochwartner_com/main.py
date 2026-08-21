import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.constanzehochwartner.com/'
CALENDAR_URL = f'{SOURCE_URL}calendar'
ICAL_URL = (
    'https://calendar.google.com/calendar/ical/'
    'c6c5807a8f34231d73e2c052399e7c35aed6fa502520da17e6527a2095997ce0%40'
    'group.calendar.google.com/public/basic.ics'
)
SOURCE = 'Constanze Hochwartner'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/calendar,text/plain;q=0.9,*/*;q=0.8',
}

COUNTRIES = {
    'Austria': 'AT',
    'Germany': 'DE',
    'United States': 'US',
}

# Google exports timed events as UTC. These state mappings convert the instant
# back to the local advertised time for the locations currently in the feed.
US_TIMEZONES = {
    'MI': 'America/Detroit',
    'TX': 'America/Chicago',
}


def unfold_ical(text):
    return re.sub(r'\r?\n[ \t]', '', text)


def unescape_ical(value):
    return (
        value.replace('\\n', '\n')
        .replace('\\N', '\n')
        .replace('\\,', ',')
        .replace('\\;', ';')
        .replace('\\\\', '\\')
        .strip()
    )


def event_fields(block):
    fields = {}
    for line in block.splitlines():
        if ':' not in line:
            continue
        name, value = line.split(':', 1)
        key = name.split(';', 1)[0]
        if key in {'SUMMARY', 'DESCRIPTION', 'LOCATION', 'DTSTART', 'UID'}:
            fields[key] = unescape_ical(value)
            if key == 'DTSTART':
                fields['DTSTART_PARAMS'] = name
    return fields


def parse_location(value):
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    venue = lines[0]
    address = ' '.join(lines[1:])
    country_name = next(
        (name for name in COUNTRIES if address == name or address.endswith(f', {name}')),
        None,
    )
    country_code = COUNTRIES.get(country_name)
    if not country_code:
        return None
    address = re.sub(rf',?\s*{re.escape(country_name)}$', '', address).strip()
    city = None
    timezone_name = None

    if country_code == 'US':
        match = re.search(r',\s*([^,]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?$', address)
        if match:
            city = match.group(1).strip()
            timezone_name = US_TIMEZONES.get(match.group(2))
    elif country_code in {'AT', 'DE'}:
        match = re.search(r'\b\d{4,5}\s+([^,]+)$', address)
        if match:
            city = match.group(1).strip()
        timezone_name = 'Europe/Vienna' if country_code == 'AT' else 'Europe/Berlin'

    if not venue or not city:
        return None
    return venue, city, country_code, timezone_name


def parse_start(fields, timezone_name):
    value = fields.get('DTSTART', '')
    params = fields.get('DTSTART_PARAMS', '')
    if 'VALUE=DATE' in params:
        try:
            event_date = datetime.strptime(value, '%Y%m%d').date().isoformat()
        except ValueError:
            return None
        return event_date, None

    try:
        if value.endswith('Z'):
            moment = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            if timezone_name:
                moment = moment.astimezone(ZoneInfo(timezone_name))
        else:
            moment = datetime.strptime(value, '%Y%m%dT%H%M%S')
    except ValueError:
        return None
    return moment.date().isoformat(), moment.strftime('%H:%M')


def time_from_title(title):
    match = re.search(r'\((\d{1,2})(?::(\d{2}))?\s*([ap]m)\)\s*$', title, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def make_record(fields):
    title = fields.get('SUMMARY', '').strip()
    location = parse_location(fields.get('LOCATION', ''))
    if not title or not location:
        return None
    venue, city, country_code, timezone_name = location
    start = parse_start(fields, timezone_name)
    if not start:
        return None
    event_date, time_from = start
    if time_from is None:
        time_from = time_from_title(title)

    description = fields.get('DESCRIPTION', '').strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': CALENDAR_URL,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    response = requests.get(ICAL_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    records = []
    for block in unfold_ical(response.text).split('BEGIN:VEVENT')[1:]:
        try:
            record = make_record(event_fields(block.split('END:VEVENT', 1)[0]))
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse calendar event',
                event='crawler_item_failed',
                level='warning',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ConstanzeHochwartnerComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='constanzehochwartner_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ConstanzeHochwartnerComCrawler().run()


if __name__ == '__main__':
    main()
