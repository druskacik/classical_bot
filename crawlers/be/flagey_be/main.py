import json
import re
from datetime import datetime
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.flagey.be/'
AGENDA_URL = urljoin(SOURCE_URL, 'en/agenda')
SOURCE = 'Flagey'
CITY = 'Brussels'
MUSIC_GENRE_ID = '49'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def agenda_url(page):
    query = urlencode({
        'start': '2000-01-01',
        'end': '',
        'genres[]': MUSIC_GENRE_ID,
        'max': 54,
        'page': page,
    })
    return f'{AGENDA_URL}?{query}'


def detail_urls(session):
    urls = []
    page = 1
    while True:
        url = agenda_url(page)
        soup = get_soup(session, url)
        page_urls = [
            urljoin(SOURCE_URL, link.get('href', ''))
            for link in soup.select('main li.eventCard a.desc[href]')
        ]
        if not page_urls:
            break
        urls.extend(page_urls)
        next_link = soup.select_one(f'a[href*="page={page + 1}"]')
        if not next_link:
            break
        page += 1
    return list(dict.fromkeys(urls))


def event_json(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        events.extend(item for item in items if item.get('@type') == 'Event')
    return events


def descriptions(soup):
    selectors = (
        '.programmeWrapper .richtext, .creditsWrapper .richtext, '
        '.desc1Wrapper .richtext, .desc2Wrapper .richtext'
    )
    parts = [clean_text(node) for node in soup.select(selectors)]
    return '\n\n'.join(dict.fromkeys(part for part in parts if part)) or None


def venues(soup):
    values = [clean_text(node) for node in soup.select('.subshows .subshow .venue')]
    return [value for value in values if value]


def parse_start(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def detail_records(session, url):
    soup = get_soup(session, url)
    events = event_json(soup)
    location_values = venues(soup)
    description = descriptions(soup)
    records = []
    for index, event in enumerate(events):
        start = parse_start(event.get('startDate'))
        title = clean_text(event.get('name'))
        venue = location_values[index] if index < len(location_values) else ''
        if not venue:
            location = event.get('location') or {}
            venue = clean_text(location.get('name'))
        if not start or not title or not venue:
            continue
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'BE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in detail_urls(session):
        try:
            records.extend(detail_records(session, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Flagey event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class FlageyBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='flagey_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
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
    FlageyBeCrawler().run()


if __name__ == '__main__':
    main()
