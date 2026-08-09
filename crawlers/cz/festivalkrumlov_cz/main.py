import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivalkrumlov.cz/'
SOURCE = 'Festival Krumlov'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.6',
}

# Older venue posts are no longer returned by the venue collection endpoint,
# although the corresponding archived events remain published.
LEGACY_VENUES = {
    164: ('jezírko, zámecký park', 'Český Krumlov'),
    165: ('Prokyšův sál', 'Český Krumlov'),
    6652: ('Kulturní centrum Prádelna', 'Český Krumlov'),
    6664: ('kostel Nanebevzetí Panny Marie ve Zlaté Koruně', 'Zlatá Koruna'),
    6714: ('Egon Schiele Art Centrum', 'Český Krumlov'),
    8975: ('Městské divadlo', 'Český Krumlov'),
    10341: ('Jezuitský sál, Hotel Růže', 'Český Krumlov'),
    11069: ('městský park', 'Český Krumlov'),
    11258: ('Svatý Kámen', 'Dolní Dvořiště'),
    14204: ('Molo Lipno', 'Lipno nad Vltavou'),
    14699: ('náměstí Svornosti', 'Český Krumlov'),
    15652: ('Studijní centrum, zámek', 'Český Krumlov'),
    17338: ('kostel sv. Petra a Pavla, Kaplice', 'Kaplice'),
    17548: ('Základní škola Plešivec', 'Český Krumlov'),
    17718: ('náměstí Svornosti', 'Český Krumlov'),
    18546: ('zámecký park', 'Český Krumlov'),
    18845: ('kostel sv. Víta', 'Český Krumlov'),
    19776: ('Kendeho vila', 'České Budějovice'),
    20768: ('salónek Rotary, Hotel Růže', 'Český Krumlov'),
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_for_venue(title):
    normalized = title.casefold()
    if 'kaplice' in normalized:
        return 'Kaplice'
    if 'kájov' in normalized:
        return 'Kájov'
    if 'boletice' in normalized:
        return 'Boletice'
    if 'kuklov' in normalized:
        return 'Kuklov'
    if 'červený dvůr' in normalized:
        return 'Chvalšiny'
    # The remaining venues currently published by this local festival are
    # explicitly described by their venue pages as being in Český Krumlov.
    return 'Český Krumlov'


def get_collection(session, endpoint):
    records = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{endpoint}',
            params={'per_page': 100, 'page': page, 'lang': 'cs'},
            timeout=45,
        )
        response.raise_for_status()
        records.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


def get_venues(session):
    venues = dict(LEGACY_VENUES)
    for item in get_collection(session, 'venue'):
        venue_id = item.get('id')
        title = clean_html(item.get('title', {}).get('rendered'))
        if venue_id and title:
            venues[venue_id] = (title, city_for_venue(title))
    return venues


def parse_event(item, venues):
    fields = item.get('acf') or {}
    title = clean_html(item.get('title', {}).get('rendered'))
    url = str(item.get('link') or '').strip()
    venue = venues.get(fields.get('event_venue'))
    raw_date = str(fields.get('event_date') or '').strip()
    if not title or not url or not venue or not raw_date:
        return None

    try:
        event_datetime = datetime.strptime(raw_date, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            event_datetime = datetime.strptime(raw_date, '%Y-%m-%d %H:%M')
        except ValueError:
            return None

    description_parts = [
        fields.get('subheadline'),
        fields.get('description'),
        fields.get('performing'),
        fields.get('programme'),
        item.get('content', {}).get('rendered'),
    ]
    description = '\n\n'.join(
        text for text in (clean_html(part) for part in description_parts) if text
    ) or None

    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': url,
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue[0],
        'city': venue[1],
        'country_code': 'CZ',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    venues = get_venues(session)
    records = []
    skipped = 0
    for item in get_collection(session, 'event'):
        record = parse_event(item, venues)
        if record:
            records.append(record)
        else:
            skipped += 1

    if skipped:
        log_message(
            'Skipped Festival Krumlov events with incomplete required fields',
            event='crawler_items_skipped',
            level='warning',
            record_count=skipped,
        )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FestivalKrumlovCzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalkrumlov_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
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
    FestivalKrumlovCzCrawler().run()


if __name__ == '__main__':
    main()
