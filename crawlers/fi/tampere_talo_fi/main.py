import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tampere-talo.fi/'
SOURCE = 'Tampere-talo'
CITY = 'Tampere'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
REST_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# These first-party genres can contain events accepted by the project. Several
# are deliberately broad, so records go through potential-event classification.
CANDIDATE_GENRE_IDS = (
    45,   # Elokuva
    53,   # Rock & Pop (can include orchestral crossover)
    63,   # Jazz, blues ja soul (can include orchestral crossover)
    69,   # Musikaali ja musiikkiteatteri
    71,   # Baletti
    73,   # Ooppera ja operetti
    357,  # Festivaali
    443,  # Tanssi
    458,  # Viihdekonsertti
    460,  # Klassinen musiikki
    464,  # Perhetapahtuma
    470,  # Orkesterimusiikki
    651,  # Kuoromusiikki
    659,  # Joulu
    1217, # Kansainvalinen vierailu
    1311, # Etno ja folk (can include early/traditional art music)
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(url, params):
    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=60)
            response.raise_for_status()
            return response.json(), response.headers
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise last_error


def current_events():
    """Read every future occurrence from the calendar's public AJAX API."""
    events = []
    page = 1
    while True:
        data, _ = get_json(
            AJAX_URL,
            {
                'action': 'em_event_feed',
                'per_page': 50,
                'page': page,
                'layout[]': 'list',
                'search': '',
            },
        )
        if not data:
            break
        events.extend(data)
        if len(data) < 50:
            break
        page += 1
    return events


def archived_candidates():
    """Use the REST taxonomy to retain published past candidate event pages."""
    posts = {}
    page = 1
    while True:
        data, headers = get_json(
            REST_URL,
            {
                'per_page': 100,
                'page': page,
                'em_genre': ','.join(map(str, CANDIDATE_GENRE_IDS)),
                '_fields': 'id,link,title,em_genre',
            },
        )
        for post in data:
            if '/tapahtuma/' in post.get('link', ''):
                posts[post['id']] = post
        if page >= int(headers.get('X-WP-TotalPages', page)):
            break
        page += 1
    return list(posts.values())


def parse_api_event(event):
    try:
        event_datetime = datetime.strptime(event['date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError):
        return None

    locations = event.get('location') or []
    if 39 in locations:
        venue_root = 'Tampere-talo'
    elif 37 in locations:
        venue_root = 'Nokia Arena'
    elif 36 in locations:
        venue_root = 'Muumimuseo'
    elif 38 in locations:
        venue_root = 'Tampere Filharmonia'
    else:
        return None
    room = clean_text(event.get('location_room'))
    venue = f'{venue_root}, {room}' if room else venue_root
    title = clean_text(event.get('title'))
    url = event.get('link')
    if not title or not url:
        return None
    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': url,
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'FI',
        'description': clean_text(event.get('post_excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_detail(post):
    response = requests.get(post['link'], headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    header = soup.select_one('.single-header')
    if not header:
        return None

    title = clean_text(header.select_one('h1')) or clean_text(post.get('title', {}).get('rendered'))
    meta_values = [clean_text(node) for node in header.select('.meta-fields .text-large')]
    date_text = next((value for value in meta_values if re.search(r'\d{1,2}\.\d{1,2}\.\d{4}', value)), '')
    venue_text = next((value for value in meta_values if value != date_text), '')
    date_match = re.search(r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b', date_text)
    time_match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', date_text)
    if not date_match:
        return None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None

    venue = venue_text
    if not venue and 'Tampere-talo' in clean_text(header):
        venue = 'Tampere-talo'
    if not all((title, venue)):
        return None

    description = clean_text(soup.select_one('.entry__content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': post['link'],
        'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        'venue': venue,
        'city': CITY,
        'country_code': 'FI',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events():
    records = [record for event in current_events() if (record := parse_api_event(event))]
    current_urls = {record['url'] for record in records}
    posts = [post for post in archived_candidates() if post['link'] not in current_urls]

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(parse_detail, post): post['link'] for post in posts}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Tampere-talo event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'], record['city'])
        existing = unique.get(key)
        if not existing or (record.get('description') and not existing.get('description')):
            unique[key] = record
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TampereTaloFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tampere_talo_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    TampereTaloFiCrawler().run()


if __name__ == '__main__':
    main()
