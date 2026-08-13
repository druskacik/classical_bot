import html
import re
from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fondazioneguidodarezzo.com/'
SOURCE = "Fondazione Guido d'Arezzo"
ICAL_URLS = (
    f'{SOURCE_URL}?post_type=tribe_events&ical=1&eventDisplay=list',
    f'{SOURCE_URL}?post_type=tribe_events&ical=1&eventDisplay=past',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/calendar,text/plain;q=0.9,*/*;q=0.8',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def unfold_ical(text):
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    unfolded = []
    for line in lines:
        if line.startswith((' ', '\t')) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def unescape_ical(value):
    value = html.unescape(value)
    value = re.sub(r'\\[nN]', '\n', value)
    value = value.replace(r'\,', ',').replace(r'\;', ';').replace(r'\\', '\\')
    value = value.replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_ical(text):
    events = []
    current = None
    for line in unfold_ical(text):
        if line == 'BEGIN:VEVENT':
            current = {}
        elif line == 'END:VEVENT':
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ':' in line:
            key, value = line.split(':', 1)
            name = key.split(';', 1)[0]
            current.setdefault(name, unescape_ical(value))
    return events


def parse_start(value):
    match = re.fullmatch(r'(\d{8})(?:T(\d{2})(\d{2})(?:\d{2})?Z?)?', value or '')
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%Y%m%d').date().isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(2) is not None:
        time_from = f'{match.group(2)}:{match.group(3)}'
    return event_date, time_from


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 2:
        return None

    venue = parts[0]
    country_names = {'italia', 'italy'}
    end = len(parts) - 1 if parts[-1].casefold() in country_names else len(parts)
    location_parts = parts[1:end]

    city = None
    for part in reversed(location_parts):
        if re.fullmatch(r'\d{5}', part):
            continue
        if re.fullmatch(r'[A-Z]{2}', part):
            continue
        if re.search(r'\d', part):
            continue
        city = part
        break

    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city


def event_record(event):
    title = event.get('SUMMARY', '').strip()
    url = event.get('URL', '').strip()
    start = parse_start(event.get('DTSTART'))
    location = parse_location(event.get('LOCATION', ''))
    if not title or not url or not start or not location:
        return None

    event_date, time_from = start
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': event.get('DESCRIPTION') or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FondazioneGuidoDarezzoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fondazioneguidodarezzo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        for feed_url in ICAL_URLS:
            try:
                response = session.get(feed_url, timeout=90)
                response.raise_for_status()
                events = parse_ical(response.text)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Fondazione Guido d\'Arezzo calendar feed',
                    event='crawler_fetch_failed',
                    level='error',
                    url=feed_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            skipped_count = 0
            for event in events:
                record = event_record(event)
                if record:
                    records.append(record)
                else:
                    skipped_count += 1
            if skipped_count:
                log_message(
                    'Skipped calendar entries without a usable date or location',
                    event='crawler_items_skipped',
                    level='warning',
                    url=feed_url,
                    record_count=skipped_count,
                )

        unique = {
            (row['url'], row['date'], row['time_from'], row['venue']): row
            for row in records
        }
        return sorted(
            unique.values(),
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FondazioneGuidoDarezzoComCrawler().run()


if __name__ == '__main__':
    main()
