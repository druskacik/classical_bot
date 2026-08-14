import json
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://osn.inba.gob.mx/'
SOURCE = 'Orquesta Sinfónica Nacional de México'
COUNTRY_CODE = 'MX'
CURRENT_SEASON_URL = urljoin(SOURCE_URL, 'temporada/actual')
ARCHIVE_URL = urljoin(SOURCE_URL, 'temporada/anteriores')

# The detail JSON-LD labels Teatro Morelos with the state (Michoacán) in its
# addressLocality field. The first-party season listing explicitly identifies
# this touring performance as Teatro Morelos, Morelia.
VENUE_CITY_OVERRIDES = {
    'Teatro Morelos': 'Morelia',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-MX,es;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_urls(session):
    soup = get_soup(session, ARCHIVE_URL)
    urls = {CURRENT_SEASON_URL}
    for link in soup.select('a[href*="/temporada/pasada/"][href]'):
        urls.add(urljoin(ARCHIVE_URL, link['href']).split('?', 1)[0])
    return sorted(urls)


def concert_urls(session):
    urls = set()
    for season_url in season_urls(session):
        soup = get_soup(session, season_url)
        for link in soup.select('a[href*="/temporada/concierto/"][href]'):
            urls.add(urljoin(season_url, link['href']).split('?', 1)[0])
    return sorted(urls)


def music_events(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text() or '')
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        events.extend(
            item for item in candidates
            if isinstance(item, dict) and item.get('@type') in {'MusicEvent', 'Event'}
        )
    return events


def programme_description(soup):
    title = soup.select_one('.titconcert')
    container = title.parent if title else soup.select_one('.nuevo')
    if not container:
        return None

    sections = []
    for element in container.select('span[style*="color"]'):
        text = clean_text(element)
        if text and text.upper() != 'INFORMACIÓN GENERAL':
            sections.append(text)
    return '\n\n'.join(dict.fromkeys(sections)) or None


def detail_records(session, url):
    soup = get_soup(session, url)
    description = programme_description(soup)
    records = []
    for event in music_events(soup):
        start = str(event.get('startDate') or '')
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?', start)
        location = event.get('location') if isinstance(event.get('location'), dict) else {}
        address = location.get('address') if isinstance(location.get('address'), dict) else {}
        title = clean_text(event.get('name'))
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        city = VENUE_CITY_OVERRIDES.get(venue, city)
        if not (match and title and venue and city):
            continue
        records.append({
            'title': title,
            'date': match.group(1),
            'url': url,
            'time_from': match.group(2),
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description or clean_text(event.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class OsnInbaGobMxCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osn_inba_gob_mx',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
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
        for url in concert_urls(session):
            try:
                records.extend(detail_records(session, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape OSN concert',
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
        result = sorted(
            unique.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )
        log_message(
            'OSN scrape completed',
            event='crawler_scrape_completed',
            record_count=len(result),
        )
        return result


def main():
    OsnInbaGobMxCrawler().run()


if __name__ == '__main__':
    main()
