import re
from datetime import date
from html import unescape
import json

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestrarossini.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Orchestra Sinfonica G. Rossini'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}
MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}
DATE_RE = re.compile(
    r'\b(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|'
    r'agosto|settembre|ottobre|novembre|dicembre)\s+(20\d{2})\b', re.I,
)
TIME_RE = re.compile(r'\b(?:ore\s*)?(\d{1,2})[.:](\d{2})\b', re.I)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,mec_category',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return events
        page += 1


def parse_location(text):
    text = clean_text(text).strip(' |-')
    parts = [part.strip() for part in re.split(r'\s*[|–—]\s*', text) if part.strip()]
    if len(parts) < 2:
        return None
    venue = parts[0]
    city = parts[-1]
    city = re.sub(r'^(?:a|ad)\s+', '', city, flags=re.I).strip()
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    if len(city) > 60 or re.search(r'\d|\b(?:ore|euro|bigliett)\b', city, re.I):
        return None
    return venue, city


def parse_detail(html, event):
    soup = BeautifulSoup(html, 'html.parser')
    event_root = soup.select_one('.mec-single-event') or soup
    date_nodes = event_root.select('.data-evento')
    date_node = next((node for node in date_nodes if DATE_RE.search(clean_text(node))), None)
    if date_node is None:
        return parse_jsonld(soup, event)

    date_text = clean_text(date_node)
    match = DATE_RE.search(date_text)
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1)),
        ).isoformat()
    except (AttributeError, KeyError, ValueError):
        return None

    location_node = date_node.find_previous(
        lambda tag: tag.name == 'div' and 'sotto-tit-evento' in tag.get('class', [])
    )
    location = parse_location(clean_text(location_node))
    if location is None:
        return None

    title = clean_text(BeautifulSoup(event['title']['rendered'], 'html.parser'))
    title = re.sub(r'^\[(?:sold out|posti disponibili|rinviato[^]]*)\]\s*', '', title, flags=re.I)
    if not title:
        return None

    time_match = TIME_RE.search(date_text)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description = clean_text(event_root)
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': event['link'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_jsonld(soup, event):
    """Parse legacy MEC pages whose original page-builder markup no longer renders."""
    data = None
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            candidate = json.loads(node.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        if candidate.get('@type') == 'Event':
            data = candidate
            break
    if data is None:
        return None

    start = data.get('startDate', '')
    match = re.match(r'(20\d{2}-\d{2}-\d{2})T(\d{2}):(\d{2})', start)
    if not match:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None

    location = data.get('location') or {}
    name = clean_text(location.get('name', '')).strip(' ,-')
    address = clean_text(location.get('address', ''))
    parts = [part.strip() for part in name.split(',') if part.strip()]
    if len(parts) >= 2:
        venue, city = parts[0], parts[-1]
    else:
        city_match = re.search(r'\b\d{5}\s+([^,(]+)', address)
        if not name or not city_match:
            return None
        venue, city = name, city_match.group(1).strip()
    city = re.sub(r'\s*\([^)]*\)\s*$', '', city).strip()
    if not venue or not city or venue.casefold() == city.casefold():
        return None

    title = clean_text(BeautifulSoup(event['title']['rendered'], 'html.parser'))
    description = clean_text(data.get('description')) or None
    if not title:
        return None
    return {
        'title': title,
        'date': match.group(1),
        'url': event['link'],
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OrchestraRossiniItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestrarossini_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = api_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Orchestra Rossini event API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            url = event.get('link')
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_detail(response.content, event)
                if record:
                    records.append(record)
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Orchestra Rossini event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    OrchestraRossiniItCrawler().run()


if __name__ == '__main__':
    main()
