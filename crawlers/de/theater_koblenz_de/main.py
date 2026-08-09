import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://theater-koblenz.de/'
API_URL = f'{SOURCE_URL}wp-json/leporello/v1/items'
SOURCE = 'Theater Koblenz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Referer': SOURCE_URL,
}

# The calendar includes guest performances outside Koblenz.  Keep the venue
# exactly as published, but resolve its city explicitly rather than silently
# assigning the theatre's home city to touring performances.
VENUE_CITIES = {
    'Stadthalle Vallendar': ('Vallendar', 'DE'),
    'Theater Lahnstein': ('Lahnstein', 'DE'),
    'Rheinfelshalle St. Goar': ('Sankt Goar', 'DE'),
    'Rheinfelshalle Sankt Goar': ('Sankt Goar', 'DE'),
    'Theater Trier': ('Trier', 'DE'),
    'Forum Leverkusen': ('Leverkusen', 'DE'),
    'Teo Otto Theater Remscheid': ('Remscheid', 'DE'),
    'Nienburger Kulturwerk': ('Nienburg/Weser', 'DE'),
}

LOCAL_VENUES = {
    'Probebühne 2',
    'Theaterzelt',
    'Probebühne 4',
    'PSD Foyer',
    'S/KO Schauspielschule Koblenz',
    'Ballettsaal',
    'Großes Haus',
    'Kaisersaal',
    'Mittelrhein-Museum',
    'Stadtbibliothek',
    'Foyer Theaterzelt',
    'Pfarrkirche St. Elisabeth',
    'Altlöhrtor',
    'Festung Ehrenbreitstein',
    'Oberes Foyer',
    'vhs Koblenz',
    'Herz-Jesu-Kirche',
    'Rhein-Mosel-Halle',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def location_for(venue):
    venue = clean_text(venue)
    if not venue:
        return None
    if venue in VENUE_CITIES:
        city, country_code = VENUE_CITIES[venue]
        return venue, city, country_code
    if venue in LOCAL_VENUES:
        return venue, 'Koblenz', 'DE'
    # Unknown locations are deliberately skipped: new touring locations must
    # not inherit Koblenz, and a bare city name is not a defensible venue.
    return None


def api_items(session):
    offset = 0
    limit = 100
    while True:
        response = session.get(
            API_URL,
            params={
                'emptyDays': 'false',
                'type': 'all',
                'search': '',
                # This endpoint's currently published archive begins in 2022.
                'startDate': '20200101',
                'limit': limit,
                'offset': offset,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        days = payload.get('days') or []
        for day in days:
            for item in day.get('items') or []:
                yield day.get('date'), item

        pagination = payload.get('pagination') or {}
        count = pagination.get('count', 0)
        total = pagination.get('total', 0)
        if not count or offset + count >= total:
            break
        offset += count


def detail_description(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'lxml')
    section = soup.select_one('main .p-production__description')
    if not section:
        section = soup.select_one('main .p-single-event__description')
    return clean_text(section.get_text('\n', strip=True)) if section else None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for event_date, item in api_items(session):
        title = clean_text(item.get('title'))
        url = item.get('permalink')
        location = location_for(item.get('location'))
        try:
            event_date = date.fromisoformat(event_date).isoformat()
        except (TypeError, ValueError):
            continue
        if not title or not isinstance(url, str) or not url.startswith('http') or not location:
            continue

        venue, city, country_code = location
        fallback_description = '\n'.join(
            value
            for value in (
                clean_text(item.get('subtitle')),
                clean_text(item.get('additionalInfos')),
            )
            if value
        ) or None
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': clean_text(item.get('startTime')) or None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': fallback_description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )

    descriptions = {}
    urls = sorted({record['url'] for record in records})
    # Detail pages contain large image-heavy documents, so keep concurrency
    # conservative to bound memory use during a full archive scrape.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(detail_description, url): url for url in urls}
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
        record['description'] = descriptions.get(record['url']) or record['description']
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class TheaterKoblenzDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_koblenz_de',
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
        dedupe_subset=['date', 'time_from', 'url', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterKoblenzDeCrawler().run()


if __name__ == '__main__':
    main()
