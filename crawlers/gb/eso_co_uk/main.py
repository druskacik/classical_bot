import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://eso.co.uk/'
WHATSON_URL = f'{SOURCE_URL}whats-on/'
PAST_URL = f'{WHATSON_URL}past-events/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
POSTS_API = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'English Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# Essential Grid uses this stable grid token when requesting the remaining
# cards from the first-party What's On grid.
GRID_TOKEN = '4fc790f363'

CITY_NAMES = (
    'Tunbridge Wells', 'Tenbury Wells', 'Chipping Campden', 'Stourport on Severn',
    'Kidderminster', 'Cheltenham', 'Birmingham', 'Bromsgrove', 'Stourbridge',
    'Droitwich', 'Wichenford', 'Leominster', 'Monmouth', 'Worcester', 'Hereford',
    'Ledbury', 'Evesham', 'Offenham', 'Bromyard', 'Whitbourne', 'Bristol',
    'Oxford', 'Rugby', 'Malvern', 'Wychbold', 'Eastnor', 'Wells', 'London',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=60, **kwargs)
    response.raise_for_status()
    return response


def parse_date(value):
    # A date range on this feed is normally an ESO Youth course rather than a
    # concrete public performance occurrence.
    if re.search(r'\d(?:st|nd|rd|th)?\s*-\s*\d', value, re.IGNORECASE):
        return None
    value = re.sub(
        r'^(?:Mon|Monday|Tue|Tues|Tuesday|Wed|Wednesday|Thu|Thur|Thurs|Thursday|'
        r'Fri|Friday|Sat|Saturday|Sun|Sunday)\s+',
        '',
        value.strip(),
        flags=re.IGNORECASE,
    )
    value = value.split(',', 1)[0].strip()
    value = re.sub(r'(\d{1,2})(?:st|nd|rd|th)\b', r'\1', value, flags=re.IGNORECASE)
    for pattern in ('%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', value, re.IGNORECASE)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def city_from_location(location):
    lowered = location.casefold()
    for city in CITY_NAMES:
        if re.search(rf'\b{re.escape(city.casefold())}\b', lowered):
            return 'Malvern' if city == 'Wychbold' else city
    return None


def venue_from_location(location):
    venue = location.split(',', 1)[0].strip()
    if not venue or re.match(r'^\d', venue):
        return None
    if re.search(r'\b(?:road|street|lane|way|avenue)\b', venue, re.IGNORECASE):
        return None
    return venue


def parse_cards(content):
    soup = BeautifulSoup(content, 'html.parser')
    cards = []
    for item in soup.select('li.eg-whats-on-wrapper'):
        link = item.select_one('a.eg-whats-on-element-45')
        date_node = item.select_one('.eg-whats-on-element-47')
        location_node = item.select_one('.eg-whats-on-element-50')
        if not link or not date_node or not location_node:
            continue
        date_text = clean_text(date_node)
        location = clean_text(location_node)
        event_date = parse_date(date_text)
        venue = venue_from_location(location)
        city = city_from_location(location)
        post_match = re.search(r'eg-post-id-(\d+)', ' '.join(item.get('class', [])))
        if not event_date or not venue or not city or not post_match:
            continue
        cards.append({
            'post_id': post_match.group(1),
            'title': clean_text(link),
            'date': event_date,
            'url': link.get('href'),
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
        })
    return cards


def load_current_cards(session, initial_content):
    cards = parse_cards(initial_content)
    initial_soup = BeautifulSoup(initial_content, 'html.parser')
    known_ids = []
    for item in initial_soup.select('li.eg-whats-on-wrapper'):
        match = re.search(r'eg-post-id-(\d+)', ' '.join(item.get('class', [])))
        if match:
            known_ids.append(match.group(1))
    try:
        response = session.post(
            AJAX_URL,
            data={
                'action': 'Essential_Grid_Front_request_ajax',
                'client_action': 'load_more_items',
                'token': GRID_TOKEN,
                'data[]': known_ids,
                'gridid': '2',
            },
            timeout=60,
        )
        response.raise_for_status()
        cards.extend(parse_cards(response.json().get('data', '')))
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to load additional ESO current-event cards',
            event='crawler_page_failed',
            level='warning',
            url=AJAX_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
    return cards


def descriptions_by_id(session, post_ids):
    descriptions = {}
    batches = [post_ids[index:index + 100] for index in range(0, len(post_ids), 100)]

    def fetch(batch):
        response = get_response(
            session,
            POSTS_API,
            params={
                'include': ','.join(batch),
                'per_page': 100,
                '_fields': 'id,content',
            },
        )
        return response.json()

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch, batch): batch for batch in batches}
        for future in as_completed(futures):
            try:
                posts = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to load ESO event descriptions',
                    event='crawler_page_failed',
                    level='warning',
                    url=POSTS_API,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for post in posts:
                soup = BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser')
                text = clean_text(soup)
                text = re.sub(r'\[(?:/?et_pb|/?fusion|/?vc_)[^\]]*\]', '', text)
                text = re.sub(r'\n{3,}', '\n\n', text).strip()
                descriptions[str(post['id'])] = text or None
    return descriptions


def recent_api_cards(session):
    """Find newly published events even before the grid's cached HTML updates."""
    response = get_response(
        session,
        POSTS_API,
        params={'per_page': 100, 'page': 1, '_fields': 'id,link,title,content'},
    )
    cards = []
    for post in response.json():
        soup = BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser')
        date_node = soup.select_one('.tribe-event-date-start')
        date_text = clean_text(date_node)
        if not date_text:
            match = re.search(
                r'\b(?:Mon|Monday|Tue|Tues|Tuesday|Wed|Wednesday|Thu|Thur|Thurs|Thursday|'
                r'Fri|Friday|Sat|Saturday|Sun|Sunday)?\s*\d{1,2}(?:st|nd|rd|th)?\s+'
                r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
                r'Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}'
                r'(?:\s*,\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?',
                clean_text(soup),
                re.IGNORECASE,
            )
            date_text = match.group(0) if match else ''
        event_date = parse_date(date_text)
        location = ''
        for heading in soup.find_all(['h3', 'h4']):
            if clean_text(heading).casefold() != 'venue':
                continue
            next_node = heading.find_next('p')
            location = clean_text(next_node)
            break
        venue = venue_from_location(location)
        city = city_from_location(location)
        if not event_date or not venue or not city:
            continue
        title = clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser'))
        cards.append({
            'post_id': str(post['id']),
            'title': title,
            'date': event_date,
            'url': post['link'],
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
        })
    return cards


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    current = get_response(session, WHATSON_URL).content
    past = get_response(session, PAST_URL).content
    cards = load_current_cards(session, current) + recent_api_cards(session) + parse_cards(past)

    unique = {}
    for card in cards:
        key = (card['title'], card['date'], card['time_from'], card['venue'])
        unique[key] = card

    descriptions = descriptions_by_id(session, list({card['post_id'] for card in unique.values()}))
    records = []
    for card in unique.values():
        records.append({
            'title': card['title'],
            'date': card['date'],
            'url': card['url'],
            'time_from': card['time_from'],
            'venue': card['venue'],
            'city': card['city'],
            'country_code': 'GB',
            'description': descriptions.get(card['post_id']),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class EsoCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eso_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    EsoCoUkCrawler().run()


if __name__ == '__main__':
    main()
