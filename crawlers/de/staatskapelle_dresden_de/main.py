import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.staatskapelle-dresden.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'konzerte-und-tickets/')
TOURS_URL = urljoin(CALENDAR_URL, 'tourneen-gastkonzerte/')
SOURCE = 'Sächsische Staatskapelle Dresden'

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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_calendar(soup):
    records = []
    for script in soup.select('main script[type="application/ld+json"]'):
        try:
            event = json.loads(script.string or '')
            start = datetime.fromisoformat(event['startDate'])
            location = event.get('location') or {}
            venue = clean_text(location.get('name'))
            title = clean_text(event.get('name'))
            url = urljoin(CALENDAR_URL, event.get('url') or '')
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue

        # The calendar deliberately labels tours as "extern". Their real
        # cities and halls are collected from the site's dedicated tour page.
        if not title or not url or not venue or venue.casefold() == 'extern':
            continue
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': 'Dresden',
            'country_code': 'DE',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_tour_location(paragraph):
    strong_values = [clean_text(node) for node in paragraph.select('strong') if clean_text(node)]
    if not strong_values:
        return None, None
    value = strong_values[-1]
    if ',' in value:
        city, venue = (part.strip() for part in value.split(',', 1))
        return city or None, venue or None

    previous = strong_values[-2] if len(strong_values) > 1 else ''
    for city in ('Ljubljana', 'Beijing', 'Shanghai', 'Shenzhen'):
        if city.casefold() in f'{previous} {value}'.casefold():
            venue = value[len(city):].strip(' ,-') if value.casefold().startswith(city.casefold()) else value
            return city, venue
    if 'Redefin' in value:
        return 'Redefin', value
    return None, None


def parse_tours(soup):
    records = []
    date_pattern = re.compile(
        r'(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{2,4})'
        r'(?:\D{0,20}?(?P<hour>\d{1,2})(?:[.:](?P<minute>\d{2}))?\s*(?:Uhr)?)?',
        re.IGNORECASE,
    )
    for heading in soup.select('main .grid-row h2'):
        row = heading.find_parent(class_='grid-row')
        middle = row.select_one('.middle') if row else None
        if not middle:
            continue
        paragraphs = middle.select('p')
        description = clean_text(row)
        tour_title = clean_text(heading).split('\n', 1)[0]
        for index, paragraph in enumerate(paragraphs[:-1]):
            match = date_pattern.search(clean_text(paragraph))
            if not match:
                continue
            city, venue = parse_tour_location(paragraphs[index + 1])
            if not city or not venue:
                continue
            year = int(match.group('year'))
            year += 2000 if year < 100 else 0
            try:
                moment = datetime(year, int(match.group('month')), int(match.group('day')))
            except ValueError:
                continue
            hour = match.group('hour')
            minute = match.group('minute') or '00'
            records.append({
                'title': tour_title,
                'date': moment.date().isoformat(),
                'url': TOURS_URL,
                'time_from': f'{int(hour):02d}:{minute}' if hour else None,
                'venue': venue,
                'city': city,
                'country_code': 'DE',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for node in soup.select('main .spielplan-pi3.details'):
        value = clean_text(node)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = parse_calendar(get_soup(session, CALENDAR_URL))
    records.extend(parse_tours(get_soup(session, TOURS_URL)))

    detail_urls = {record['url'] for record in records if record['url'] != TOURS_URL}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Staatskapelle Dresden concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    for record in records:
        if record['url'] in descriptions:
            record['description'] = descriptions[record['url']]

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['city'], item['title'], item['venue']
    ))


class StaatskapelleDresdenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='staatskapelle_dresden_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    StaatskapelleDresdenDeCrawler().run()


if __name__ == '__main__':
    main()
