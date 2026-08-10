import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-bonn.de/de/'
EVENTS_URL = urljoin(SOURCE_URL, 'api/events/')
SOURCE = 'Theater Bonn'
CITY = 'Bonn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The API mixes genre and venue values in ``tags``. These are the venue tags
# currently used for Theater Bonn's own stages and partner theatres in Bonn.
VENUES = {
    'Opernhaus Bühne': 'Opernhaus Bonn',
    'Oper Foyerbühne': 'Foyerbühne, Opernhaus Bonn',
    'Schauspielhaus': 'Schauspielhaus Bonn',
    'Schauspielhaus Foyer': 'Foyer, Schauspielhaus Bonn',
    'Schauspiel Kassenfoyer': 'Kassenfoyer, Schauspielhaus Bonn',
    'Werkstatt': 'Werkstatt, Opernhaus Bonn',
    'Werkstattfoyer': 'Werkstattfoyer, Opernhaus Bonn',
    'Probebühne 1': 'Probebühne 1, Theater Bonn',
    'Probebühne 4': 'Probebühne 4, Theater Bonn',
    'Probebühne 5': 'Probebühne 5, Theater Bonn',
    'Bar 65': 'Bar 65, Theater Bonn',
    'Eingangsfoyer': 'Eingangsfoyer, Opernhaus Bonn',
    'Opernrasen': 'Opernrasen, Opernhaus Bonn',
    'Junges Theater Bonn': 'Junges Theater Bonn',
    'Theater Marabu': 'Theater Marabu',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_venue(event):
    for tag in event.get('tags') or []:
        venue = VENUES.get(clean_text(tag))
        if venue:
            return venue
    return None


def detail_description(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main.event-detail')
    if not main:
        return None

    sections = []
    head = main.select_one('.page-head')
    if head:
        sections.append(clean_text(head.get_text('\n', strip=True)))

    # The first plain grid container after the page head contains the synopsis
    # and programme notes. Cast lists, ticket dates, galleries, and magazine
    # teasers are deliberately excluded.
    body = head.find_next_sibling('div', class_='grid-container') if head else None
    if body:
        sections.append(clean_text(body.get_text('\n', strip=True)))
    sections = [section for section in sections if section]
    return '\n\n'.join(sections) or None


def api_record(event):
    title = clean_text(event.get('title'))
    venue = event_venue(event)
    path = event.get('url')
    try:
        event_date = datetime.strptime(event.get('date_full', ''), '%d.%m.%Y').date().isoformat()
    except (TypeError, ValueError):
        return None
    time_match = re.search(r'\b(\d{1,2}:\d{2})\b', event.get('date_time') or '')
    if not title or not venue or not path:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': time_match.group(1).zfill(5) if time_match else None,
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=60)
    response.raise_for_status()
    events = response.json()

    records = [record for event in events if (record := api_record(event))]
    descriptions = {}
    urls = {record['url'] for record in records}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        summary = record['description']
        if detail and summary and summary not in detail:
            record['description'] = f'{detail}\n\n{summary}'
        elif detail:
            record['description'] = detail

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class TheaterBonnDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_bonn_de',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterBonnDeCrawler().run()


if __name__ == '__main__':
    main()
