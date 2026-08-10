import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brso.de/'
API_URL = f'{SOURCE_URL}wp-json/v1/filter/concerts'
SOURCE = 'Symphonieorchester des Bayerischen Rundfunks'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

FOREIGN_COUNTRIES = {
    'Wien': 'AT', 'Grafenegg': 'AT', 'Salzburg': 'AT',
    'Tokio': 'JP', 'Nishinomiya': 'JP', 'Kawasaki-City': 'JP',
    'Nagoya-shi': 'JP', 'Seoul': 'KR', 'Taipei': 'TW', 'Taichung': 'TW',
    'Kaohsiung': 'TW', 'London': 'GB', 'Liverpool': 'GB', 'Birmingham': 'GB',
    'New York': 'US', 'Chicago': 'US', 'Washington D.C.': 'US',
    'Philadelphia': 'US', 'Aix-en-Provence': 'FR', 'Paris': 'FR',
    'Madrid': 'ES', 'Barcelona': 'ES', 'València': 'ES',
    'Santa Cruz de Tenerife': 'ES', 'Prag': 'CZ', 'Luzern': 'CH',
    'Dublin': 'IE', 'Brüssel': 'BE', 'Luxembourg': 'LU', 'Mailand': 'IT',
    'Amsterdam': 'NL',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_listing(session):
    response = session.post(API_URL, data={'abo': '0'}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    return payload.get('data', {}).get('concerts') or []


def parse_location(value):
    parts = [clean_text(part) for part in (value or '').split(',') if clean_text(part)]
    if len(parts) < 2 or parts[0].startswith('Bayerische '):
        return None
    city = parts[0]
    venue = ', '.join(parts[1:])
    if not city or not venue or city == venue:
        return None
    return city, venue, FOREIGN_COUNTRIES.get(city, 'DE')


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []

    programme_anchor = soup.select_one('#programm')
    if programme_anchor:
        section = programme_anchor.parent
        programme = clean_text(section)
        if programme:
            parts.append(programme)

    # Editorial notes are the long prose blocks on concert pages. Exclude the
    # programme's parent to avoid duplicating its composer/work listing.
    for block in soup.select('main .content-wysiwyg'):
        if programme_anchor and programme_anchor.parent in block.parents:
            continue
        text = clean_text(block)
        if len(text) >= 80 and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def make_record(event, description=None):
    title = clean_text(event.get('title'))
    subtitle = clean_text(event.get('subtitle'))
    if subtitle and subtitle.lower() not in title.lower():
        title = f'{title} – {subtitle}'
    url = clean_text(event.get('url'))
    location = parse_location(event.get('location'))
    if not title or not url or not location:
        return None
    try:
        start = datetime.fromisoformat(event.get('start') or '')
        event_date = start.date().isoformat()
    except (TypeError, ValueError):
        return None
    city, venue, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = get_listing(session)
    records = []
    descriptions = {}
    urls = sorted({clean_text(event.get('url')) for event in events if event.get('url')})
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                descriptions[url] = None
    for event in events:
        record = make_record(event, descriptions.get(clean_text(event.get('url'))))
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BrsoDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brso_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BrsoDeCrawler().run()


if __name__ == '__main__':
    main()
