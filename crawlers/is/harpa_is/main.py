import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.harpa.is/'
PROGRAM_URL = f'{SOURCE_URL}dagskra'
SOURCE = 'Harpa'
CITY = 'Reykjavík'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'is-IS,is;q=0.9,en;q=0.7',
}

# Harpa is a mixed venue. These first-party event types form a deliberately
# broad music/performance candidate feed; the potential-event classifier makes
# the final scope decision. Family, dance and festival labels are included
# because eligible orchestral family events and classical dance use them.
CANDIDATE_CATEGORIES = {
    'Ballett',
    'Börn og Fjölskyldan',
    'Dans',
    'Experimental',
    'Fjölskyldan',
    'Fjölskyldudagskrá Hörpu',
    'Heiðurstónleikar',
    'Hátíðir',
    'Jazz',
    'Jazz og blús',
    'Jól',
    'Jólatónleikar',
    'Kammertónlist',
    'Klassík',
    'Kvikmynda tónleikar',
    'Kór',
    'Menningarnótt',
    'Raftónlist',
    'Rokk og popp',
    'Sinfóníuhljómsveit',
    'Stórsveit',
    'Sígild og samtímatónlist',
    'Sígildir sunnudagar',
    'Tribute',
    'Tónleikar',
    'Tónlist',
    'Upprásin',
    'hip hop og rapp',
    'Ópera',
    'Þjóðlagatónlist',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def next_payloads(html):
    soup = BeautifulSoup(html, 'html.parser')
    pattern = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)')
    for script in soup.find_all('script'):
        for match in pattern.finditer(script.string or ''):
            try:
                yield json.loads(match.group(1))
            except json.JSONDecodeError:
                continue


def find_event_map(value):
    if isinstance(value, dict):
        if isinstance(value.get('eventsById'), dict):
            return value['eventsById']
        for child in value.values():
            found = find_event_map(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_event_map(child)
            if found is not None:
                return found
    return None


def listing_events(html):
    for payload in next_payloads(html):
        if '"eventsById"' not in payload:
            continue
        _, separator, encoded = payload.partition(':')
        if not separator:
            continue
        try:
            tree = json.loads(encoded)
        except json.JSONDecodeError:
            continue
        events = find_event_map(tree)
        if events is not None:
            return [
                event for event in events.values()
                if set(event.get('categories') or []) & CANDIDATE_CATEGORIES
            ]
    raise ValueError('Harpa event data was not found in the programme page')


def event_schema(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            value = json.loads(script.string or '')
        except json.JSONDecodeError:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def parse_occurrence(value):
    value = str(value or '').removeprefix('$D')
    match = re.match(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', value)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    return event_date, f'{match.group(2)}:{match.group(3)}'


def make_records(event, schema):
    title = clean_text(schema.get('name') or event.get('title'))
    event_id = str(event.get('id') or '')
    if not title or not event_id:
        return []

    url = f'{SOURCE_URL}vidburdir/{event_id}'
    hall = clean_text(event.get('hallName'))
    venue = f'Harpa – {hall}' if hall else 'Harpa'
    description = clean_text(schema.get('description')) or None
    occurrences = event.get('dates') or [event.get('date') or schema.get('startDate')]

    records = []
    for occurrence in occurrences:
        parsed = parse_occurrence(occurrence)
        if not parsed:
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'IS',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(get_html(session, PROGRAM_URL))
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_html, session, f'{SOURCE_URL}vidburdir/{event["id"]}'): event
            for event in events if event.get('id')
        }
        for future in as_completed(futures):
            event = futures[future]
            url = f'{SOURCE_URL}vidburdir/{event.get("id", "")}'
            try:
                schema = event_schema(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Harpa event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                schema = {}
            records.extend(make_records(event, schema))

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class HarpaIsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='harpa_is',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IS',
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
    HarpaIsCrawler().run()


if __name__ == '__main__':
    main()
