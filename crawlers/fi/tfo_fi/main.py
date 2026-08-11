import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tfo.fi/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/tapahtuma'
SOURCE = 'Turun filharmoninen orkesteri'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_all_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'lang': 'fi'},
            timeout=60,
        )
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return posts
        page += 1


def parse_performance(value):
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4}),?\s+(\d{1,2}:[0-5]\d)', value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None
    return event_date, match.group(2).zfill(5)


def location_item(soup):
    for item in soup.select('.tapahtuma-detail-item'):
        if item.select_one('.tapahtuma-esitysajat-lista'):
            continue
        if item.select_one('.tapahtuma-detail-title'):
            return item
    return None


def city_from_address(address):
    matches = re.findall(r'\b\d{5}\s+([A-Za-zÅÄÖåäöÉé-]+)', address)
    return matches[-1].title() if matches else None


def resolve_location(title, venue, address, event_date, time_from):
    searchable = f'{venue}\n{address}'
    city = city_from_address(address)

    # A small number of posts combine explicitly identified venues in one
    # location field. Resolve those per performance instead of assigning the
    # orchestra's home hall to its Helsinki appearance.
    if 'Veikkaus Arena' in searchable and 'Musiikkitalo Fuuga' in searchable:
        if event_date == '2027-03-12':
            return 'Veikkaus Arena', 'Helsinki'
        return 'Musiikkitalo Fuuga', 'Turku'
    if 'Pääskyvuoren koulu' in searchable and 'Lausteen koulu' in searchable:
        return ('Pääskyvuoren koulu', 'Turku') if event_date == '2026-05-19' else ('Lausteen koulu', 'Turku')
    if 'Palvelutalo Iso-Heikki' in searchable and 'Ruusukortteli' in searchable:
        return ('Palvelutalo Iso-Heikki', 'Turku') if time_from == '13:00' else ('Ruusukortteli', 'Turku')

    if venue.casefold() == 'vaihtuva sijainti.':
        return None, None
    if venue.casefold() == 'aninkaistenkatu 9':
        venue = 'Turun konserttitalo'
    elif venue.casefold() == 'myllynkatu 1' and 'mylly' in title.casefold():
        venue = 'Kauppakeskus Mylly'

    # TFO's event data often omits the municipality when an event is in Turku.
    # An explicit non-Turku postal city above always takes precedence.
    return venue, city or 'Turku'


def description_text(soup):
    parts = []
    for selector in ('.tapahtuma-kuvaus', '.tapahtuma-accordions'):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def parse_post(post):
    title = clean_text(BeautifulSoup(post.get('title', {}).get('rendered', ''), 'html.parser'))
    url = post.get('link', '').strip()
    soup = BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser')
    item = location_item(soup)
    venue = clean_text(item.select_one('.tapahtuma-detail-title')) if item else ''
    address = clean_text(item.select_one('.tapahtuma-detail-text')) if item else ''
    description = description_text(soup)
    records = []
    for element in soup.select('.tapahtuma-esitysaika'):
        event_date, time_from = parse_performance(clean_text(element))
        resolved_venue, city = resolve_location(title, venue, address, event_date, time_from)
        if not all((title, event_date, url, resolved_venue, city)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': resolved_venue,
            'city': city,
            'country_code': 'FI',
            'description': description,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        posts = get_all_posts(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch TFO event API',
            event='crawler_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise
    records = [record for post in posts for record in parse_post(post)]
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TfoFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tfo_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    TfoFiCrawler().run()


if __name__ == '__main__':
    main()
