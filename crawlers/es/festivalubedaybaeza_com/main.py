import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivalubedaybaeza.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/programa'
SOURCE = 'Festival de Música Antigua de Úbeda y Baeza'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        raw = str(value)
        text = (
            BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
            if '<' in raw
            else raw.strip()
        )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_events(session):
    page = 1
    events = []
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'link,title',
            },
            timeout=60,
        )
        response.raise_for_status()
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return events
        page += 1


def parse_date(value):
    match = re.fullmatch(r'\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*', value)
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), int(match.group(2)), int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_location(value):
    """Split the site's uppercase municipality from its named venue."""
    words = clean_text(value).split()
    city_words = []
    for word in words:
        letters = re.sub(r'[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ]', '', word)
        # Venue abbreviations such as "S.I. Catedral" are uppercase too, but
        # punctuation marks their boundary from the preceding municipality.
        if city_words and '.' in word:
            break
        if letters and letters == letters.upper():
            city_words.append(word)
        else:
            break
    venue_words = words[len(city_words):]
    city = ' '.join(city_words).strip(' ,-')
    venue = ' '.join(venue_words).strip(' ,-')
    if not city or not venue:
        return None
    return city.title(), venue


def event_description(soup):
    # The event's Elementor single-post root contains the synopsis, complete
    # programme, and performer notes, while excluding the global site footer.
    root = soup.select_one('.elementor-location-single.programa')
    if root is None:
        root = soup.select_one('.elementor-location-single')
    return clean_text(root) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    event_date = parse_date(clean_text(soup.select_one('.evento-fecha')))
    location = parse_location(clean_text(soup.select_one('.evento-ubicacion')))
    time_text = clean_text(soup.select_one('.evento-hora'))
    time_match = re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', time_text)
    if not title or not event_date or not location or not url:
        return None
    city, venue = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0).zfill(5) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': event_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(event):
    url = clean_text(event.get('link'))
    if not url:
        return None
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_event(response.text, url)


class FestivalUbedayBaezaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalubedaybaeza_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = api_events(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, event): event for event in events}
            for future in as_completed(futures):
                event = futures[future]
                url = clean_text(event.get('link'))
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape FeMAUB concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete FeMAUB concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, or city is missing',
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    FestivalUbedayBaezaComCrawler().run()


if __name__ == '__main__':
    main()
