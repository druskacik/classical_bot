import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.arcanisenkul.com/tr'
CALENDAR_URL = f'{SOURCE_URL}/calendar'
SOURCE = 'Arcan İsenkul'
SANITY_QUERY_URL = (
    'https://bciv18g3.api.sanity.io/v2023-05-03/data/query/production'
)
EVENT_QUERY = '''
*[_type == "event"] | order(datetime asc) {
  _id, title, datetime, venue, city, country, slug,
  description, performers, program
}
'''

COUNTRY_CODES = {
    'Fransa': 'FR',
    'France': 'FR',
    'Germany': 'DE',
    'Türkiye': 'TR',
    'Turkey': 'TR',
    'United States': 'US',
}

# A handful of early archive documents reused Frankfurt in the structured city
# field even though their venue names explicitly identify the touring location.
CITY_BY_VENUE = {
    'Kloster-Eberbach-Straße 4 65346 Eltville': 'Eltville am Rhein',
    'Rathaus Gießen': 'Gießen',
    'Festsaal des Altkönigstifts Kronberg': 'Kronberg im Taunus',
    'Alleesaal Bad Schwalbach': 'Bad Schwalbach',
    'Bürgerhaus Schwalbach': 'Schwalbach am Taunus',
    'Vilco - Kurhaus Bad Vilbel': 'Bad Vilbel',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'tr-TR,tr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def portable_text(blocks):
    paragraphs = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        text = ''.join(
            child.get('text', '')
            for child in block.get('children', [])
            if isinstance(child, dict)
        ).strip()
        if text:
            paragraphs.append(text)
    return '\n\n'.join(paragraphs)


def event_description(event):
    sections = []
    descriptions = event.get('description') or {}
    if isinstance(descriptions, dict):
        body = portable_text(
            descriptions.get('tr')
            or descriptions.get('en')
            or descriptions.get('de')
        )
        if body:
            sections.append(body)

    performers = []
    for performer in event.get('performers') or []:
        name = clean_text(performer.get('name'))
        role = clean_text(performer.get('role'))
        if name:
            performers.append(f'{role}: {name}' if role else name)
    if performers:
        sections.append('Performers\n' + '\n'.join(performers))

    programme = []
    for item in event.get('program') or []:
        composer = clean_text(item.get('composer'))
        work = clean_text(item.get('work'))
        movement = clean_text(item.get('movement'))
        line = ' — '.join(value for value in (composer, work) if value)
        if movement:
            line = f'{line}\n{movement}' if line else movement
        if line:
            programme.append(line)
    if programme:
        sections.append('Programme\n' + '\n\n'.join(programme))

    return '\n\n'.join(sections) or None


def displayed_times(session):
    response = session.get(CALENDAR_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    times = {}
    for article in soup.select('article'):
        link = article.select_one('a[href*="/calendar/"]')
        time_node = article.select_one('span.font-mono')
        if not link or not time_node:
            continue
        match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', time_node.get_text(' ', strip=True))
        if match:
            times[link.get('href', '').rstrip('/').rsplit('/', 1)[-1]] = match.group(0)
    return times


def get_events():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        SANITY_QUERY_URL,
        params={'query': EVENT_QUERY},
        timeout=60,
    )
    response.raise_for_status()
    events = response.json().get('result', [])

    try:
        times = displayed_times(session)
    except requests.RequestException as error:
        times = {}
        log_message(
            'Failed to load displayed event times',
            event='crawler_listing_failed',
            level='warning',
            url=CALENDAR_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    records = []
    for event in events:
        title = clean_text(event.get('title'))
        venue = clean_text(event.get('venue'))
        city = clean_text(event.get('city'))
        city = CITY_BY_VENUE.get(venue, city)
        country_code = COUNTRY_CODES.get(clean_text(event.get('country')))
        slug = clean_text((event.get('slug') or {}).get('current'))
        try:
            event_date = datetime.fromisoformat(
                event.get('datetime', '').replace('Z', '+00:00')
            ).date().isoformat()
        except (TypeError, ValueError):
            continue
        if not all((title, venue, city, country_code, slug)):
            continue

        url = urljoin(f'{CALENDAR_URL}/', slug)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': times.get(slug),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': event_description(event),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class ArcanisenkulComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arcanisenkul_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_events()


def main():
    ArcanisenkulComCrawler().run()


if __name__ == '__main__':
    main()
