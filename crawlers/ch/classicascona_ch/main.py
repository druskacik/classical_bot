import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://classicascona.ch/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/konzerte'
SOURCE = 'classicAscona'

VENUE_CITIES = (
    'Ronco sopra Ascona', 'Palagnedra', 'Brissago', 'Locarno',
    'Arcegno', 'Losone', 'Mogno', 'Bordei', 'Ascona',
)

HEADERS = {
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9,it;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=60, **kwargs)
    response.raise_for_status()
    return response


def concert_posts(session):
    posts = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'lang': 'en',
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'id,link,title',
            },
        )
        posts.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', 1)):
            return posts
        page += 1


def json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_objects(child)


def music_event(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in json_objects(payload):
            event_type = item.get('@type')
            if event_type == 'MusicEvent' or (
                isinstance(event_type, list) and 'MusicEvent' in event_type
            ):
                return item
    return None


def full_description(soup, event):
    parts = []
    for heading in soup.select('h2'):
        label = clean_text(heading).lower()
        if label not in {
            'concert information', 'program', 'programme',
            'informazioni sul concerto', 'programma',
            'konzertinformationen', 'programm',
        }:
            continue
        block = heading.parent
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)

    structured_description = clean_text(event.get('description'))
    if structured_description and not any(
        structured_description in part for part in parts
    ):
        parts.insert(0, structured_description)
    return '\n\n'.join(parts) or None


def parse_start_date(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def normalize_city(value, venue):
    city = clean_text(value)
    city = re.sub(
        r'\s*,\s*(?:Switzerland|Schweiz|Svizzera|Suisse)$', '', city, flags=re.I
    )
    if city:
        return city
    return next(
        (
            candidate for candidate in VENUE_CITIES
            if re.search(rf'(?<!\w){re.escape(candidate)}(?!\w)', venue, re.I)
        ),
        '',
    )


def make_record(post, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    event = music_event(soup)
    if not event:
        return None

    title = clean_text(event.get('name')) or clean_text((post.get('title') or {}).get('rendered'))
    url = event.get('url') or post.get('link') or ''
    event_date, time_from = parse_start_date(event.get('startDate'))

    locations = event.get('location') or []
    if isinstance(locations, dict):
        locations = [locations]
    location = next((item for item in locations if isinstance(item, dict)), {})
    address = location.get('address') or {}
    if not isinstance(address, dict):
        address = {}
    venue = clean_text(location.get('name'))
    city = normalize_city(address.get('addressLocality'), venue)
    country_code = clean_text(address.get('addressCountry')).upper()

    if not all((title, event_date, url, venue, city)):
        return None
    if country_code and country_code not in {
        'CH', 'CHE', 'SWITZERLAND', 'SCHWEIZ', 'SVIZZERA', 'SUISSE'
    }:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CH',
        'description': full_description(soup, event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = concert_posts(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_response, session, post.get('link', '')): post
            for post in posts
            if post.get('link')
        }
        for future in as_completed(futures):
            post = futures[future]
            try:
                record = make_record(post, future.result().text)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape classicAscona concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=post.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class ClassicasconaChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='classicascona_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ClassicasconaChCrawler().run()


if __name__ == '__main__':
    main()
