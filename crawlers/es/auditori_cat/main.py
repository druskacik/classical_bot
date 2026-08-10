import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import urllib3
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.auditori.cat/ca/'
PROGRAM_URL = f'{SOURCE_URL}esdeveniment/'
API_URL = 'https://www.auditori.cat/wp-admin/admin-ajax.php'
SOURCE = "L'Auditori"
TIMEZONE = ZoneInfo('Europe/Madrid')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ca-ES,ca;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def new_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    # The server currently presents an incomplete certificate chain to some
    # Linux trust stores. Browsers accept it, so requests must do the same.
    session.verify = False
    return session


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def listing_events(session):
    # The AJAX endpoint requires the PHP session cookie set by the archive.
    get_response(session, PROGRAM_URL)
    events = []
    last_date = 'false'
    seen_ids = set()

    while True:
        params = {
            'action': 'get_auditori_events_query',
            'page': 1,
            'limit': 100,
            'output_profile': 'basic_card',
            'from_date': last_date,
            'hide_in_page': 'true',
        }
        batch = get_response(session, API_URL, params=params).json()
        if not isinstance(batch, list) or not batch:
            break

        fresh = [event for event in batch if event.get('id') not in seen_ids]
        if not fresh:
            break
        events.extend(fresh)
        seen_ids.update(event.get('id') for event in fresh)
        next_dates = [int(event['event_next_date']) for event in batch if event.get('event_next_date')]
        if len(batch) < 100 or not next_dates:
            break
        last_date = max(next_dates)

    return events


def resolve_city(venue):
    normalized = clean_text(venue).casefold()
    if not normalized:
        return None
    if 'madrid' in normalized:
        return 'Madrid'
    if 'paiporta' in normalized:
        return 'Paiporta'
    if 'tavernes de la valldigna' in normalized:
        return 'Tavernes de la Valldigna'
    if 'valència' in normalized or 'valencia' in normalized:
        return 'València'

    # These are L'Auditori rooms or explicitly named Barcelona districts and
    # venues. This home-city default is not used for named touring venues.
    barcelona_markers = (
        'sala ', 'espai 5', 'museu de la música', 'barcelona', 'nou barris',
        'montbau', 'poblenou', 'sants', 'parc de l’espanya industrial',
        'pedralbes', 'sant andreu',
        'congrés eucarístic',
    )
    if any(marker in normalized for marker in barcelona_markers):
        return 'Barcelona'
    return None


def parse_sessions(soup, fallback_timestamp=None):
    sessions = []
    seen = set()
    for option in soup.select('.a-session-option-content'):
        values = [clean_text(node.get_text(' ', strip=True)) for node in option.select('.a-events-no')]
        joined = ' '.join(values)
        match = re.search(r'(\d{2})-(\d{2})-(\d{4}).*?(\d{2}):(\d{2})', joined)
        if not match:
            continue
        try:
            event_date = datetime(
                int(match.group(3)), int(match.group(2)), int(match.group(1))
            ).date().isoformat()
        except ValueError:
            continue
        item = (event_date, f'{match.group(4)}:{match.group(5)}')
        if item not in seen:
            seen.add(item)
            sessions.append(item)

    if not sessions and fallback_timestamp:
        try:
            value = datetime.fromtimestamp(int(fallback_timestamp), TIMEZONE)
            sessions.append((value.date().isoformat(), value.strftime('%H:%M')))
        except (TypeError, ValueError, OSError):
            pass
    return sessions


def event_title(event, soup):
    title_node = soup.select_one('.a-event-block h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    if not title:
        title = clean_text((event.get('wp_post') or {}).get('post_title'))
    subtitle = clean_text(event.get('subtitle'))
    if subtitle and subtitle.casefold() not in title.casefold():
        title = f'{title} – {subtitle}'
    return title


def parse_event(event, session):
    url = event.get('link') or ''
    if not url:
        return []
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    title = event_title(event, soup)
    venue = clean_text(((event.get('hall_obj') or {}).get('wp_post') or {}).get('post_title'))
    city = resolve_city(venue)
    sessions = parse_sessions(soup, event.get('event_next_date'))
    content = soup.select_one('.entry-content.wp-block-post-content')
    description = clean_text(content.get_text('\n', strip=True) if content else '') or None

    if not title or not venue or not city or not sessions:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, event_time in sessions
    ]


def get_concerts():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = new_session()
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, event, session): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class AuditoriCatCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='auditori_cat',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AuditoriCatCrawler().run()


if __name__ == '__main__':
    main()
