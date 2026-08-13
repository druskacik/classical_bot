import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://yokohama-minatomiraihall.jp/'
CALENDAR_URL = f'{SOURCE_URL}concert/calendar.html'
CALENDAR_API = f'{SOURCE_URL}cal.json'
SOURCE = 'Yokohama Minato Mirai Hall'
CITY = 'Yokohama'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

VENUES = {
    10: 'Yokohama Minato Mirai Hall, Main Hall',
    20: 'Yokohama Minato Mirai Hall, Small Hall',
    30: 'Yokohama Minato Mirai Hall, Reception Room',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_url(event_date, event_id):
    return f'{CALENDAR_URL}#cal{event_date.replace("-", "")}{event_id}'


def resolve_venue(event):
    venue_code = event.get('venue')
    if venue_code in VENUES:
        return VENUES[venue_code]
    if venue_code == 99:
        venue_text = clean_text((event.get('ja') or {}).get('venue_text'))
        if venue_text:
            return f'Yokohama Minato Mirai Hall, {venue_text}'
    return None


def format_people(people):
    lines = []
    for person in people or []:
        name = clean_text(person.get('name'))
        part = clean_text(person.get('part'))
        if name:
            lines.append(f'{part}: {name}' if part else name)
    return lines


def format_program(program):
    lines = []
    for work in program or []:
        composer = clean_text(work.get('author'))
        title = clean_text(work.get('title'))
        if title:
            lines.append(f'{composer}: {title}' if composer else title)
    return lines


def make_description(event):
    details = event.get('ja') or {}
    parts = []
    subtitle = clean_text(details.get('subtitle'))
    if subtitle:
        parts.append(subtitle)

    people = format_people(details.get('player'))
    if people:
        parts.append('出演\n' + '\n'.join(people))

    program = format_program(details.get('program'))
    if program:
        parts.append('曲目\n' + '\n'.join(program))

    comment = clean_text(event.get('comment'))
    if comment:
        parts.append(comment)
    return '\n\n'.join(parts) or None


def make_record(event_date, event):
    details = event.get('ja') or {}
    title = clean_text(details.get('title')).replace('\n', ' ')
    venue = resolve_venue(event)
    event_id = event.get('id')
    if not title or not venue or event_id is None:
        return None
    try:
        normalized_date = date.fromisoformat(event_date).isoformat()
    except (TypeError, ValueError):
        return None

    time_from = clean_text(event.get('start_time'))
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
        time_from = None

    return {
        'title': title,
        'date': normalized_date,
        'url': event_url(normalized_date, event_id),
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'JP',
        'description': make_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    response = requests.get(CALENDAR_API, headers=HEADERS, timeout=60)
    response.raise_for_status()
    payload = response.json()
    records = []
    skipped = 0
    for event_date, day in (payload.get('cal') or {}).items():
        for event in day.get('concerts') or []:
            record = make_record(event_date, event)
            if record:
                records.append(record)
            else:
                skipped += 1
    if skipped:
        log_message(
            'Skipped calendar entries missing required fields',
            event='crawler_items_skipped',
            level='warning',
            record_count=skipped,
            url=CALENDAR_API,
        )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class YokohamaMinatomiraihallJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yokohama_minatomiraihall_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    YokohamaMinatomiraihallJpCrawler().run()


if __name__ == '__main__':
    main()
