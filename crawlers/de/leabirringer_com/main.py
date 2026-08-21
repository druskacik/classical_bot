import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.leabirringer.com/'
CONCERTS_URL = f'{SOURCE_URL}deutsch/termine/'
SOURCE = 'Lea Birringer'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
}

EVENT_HEADER = re.compile(
    r'^(?P<date>\d{2}\.\d{2}\.(?:\d{2}|\d{4})),\s*'
    r'(?:(?P<time>\d{1,2}h(?:\d{2})?),\s*)?'
    r'(?P<location>.+?)\s*\((?P<country>[A-Z]{2,3})\)$'
)
COUNTRY_CODES = {'USA': 'US'}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def content_lines(soup):
    content = soup.select_one('#content_area')
    if content is None:
        return []

    lines = [clean_text(line) for line in content.get_text('\n').splitlines()]
    lines = [line for line in lines if line]

    # Jimdo may wrap a date/location heading across multiple HTML text nodes.
    joined = []
    pending = ''
    for line in lines:
        if pending:
            pending = f'{pending} {line}'
            if re.search(r'\([A-Z]{2,3}\)$', pending):
                joined.append(pending)
                pending = ''
        elif re.match(r'^\d{2}\.\d{2}\.(?:\d{2}|\d{4}),', line) and not re.search(
            r'\([A-Z]{2,3}\)$', line
        ):
            pending = line
        else:
            joined.append(line)
    if pending:
        joined.append(pending)
    return joined


def parse_date(value):
    for pattern in ('%d.%m.%y', '%d.%m.%Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_time(value):
    if not value:
        return None
    hour, separator, minute = value.partition('h')
    try:
        return f'{int(hour):02d}:{int(minute) if separator and minute else 0:02d}'
    except ValueError:
        return None


def parse_location(value):
    parts = [clean_text(part) for part in value.split(',')]
    if len(parts) < 2:
        return '', ''
    city = parts[0]
    venue = ', '.join(parts[1:])
    if not city or not venue or city.casefold() == venue.casefold():
        return '', ''
    return city, venue


def make_title(city, description):
    first_line = next((line for line in description if line), '')
    if first_line:
        return f'Lea Birringer – {first_line}'
    return f'Lea Birringer in {city}'


def parse_events(soup):
    lines = content_lines(soup)
    positions = [(index, EVENT_HEADER.match(line)) for index, line in enumerate(lines)]
    positions = [(index, match) for index, match in positions if match]
    records = []

    for position, (index, match) in enumerate(positions):
        next_index = positions[position + 1][0] if position + 1 < len(positions) else len(lines)
        description_lines = [
            line for line in lines[index + 1:next_index]
            if not re.match(r'^(?:SAISON\b|Vita$)', line, re.I)
        ]
        city, venue = parse_location(match.group('location'))
        event_date = parse_date(match.group('date'))
        country_code = COUNTRY_CODES.get(match.group('country'), match.group('country'))

        if not event_date or not city or not venue or not re.fullmatch(r'[A-Z]{2}', country_code):
            log_message(
                'Skipped incomplete Lea Birringer event',
                event='crawler_item_skipped',
                level='warning',
                url=CONCERTS_URL,
                error_type='IncompleteEventData',
                error_message='Required date, city, venue, or country is missing',
            )
            continue

        records.append({
            'title': make_title(city, description_lines),
            'date': event_date,
            'url': CONCERTS_URL,
            'time_from': parse_time(match.group('time')),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': '\n'.join(description_lines) or None,
        })
    return records


class LeaBirringerComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='leabirringer_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        records = parse_events(BeautifulSoup(response.text, 'html.parser'))
        # The page repeats a few season-boundary concerts. Prefer the copy
        # carrying a published start time.
        deduplicated = {}
        for record in records:
            base = (record['title'], record['date'], record['venue'], record['city'])
            key = (*base, record['time_from'])
            if record['time_from']:
                deduplicated.pop((*base, None), None)
                deduplicated[key] = record
            elif not any(existing[:4] == base and existing[4] for existing in deduplicated):
                deduplicated[key] = record
        return sorted(
            deduplicated.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    LeaBirringerComCrawler().run()


if __name__ == '__main__':
    main()
