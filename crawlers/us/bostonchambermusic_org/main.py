import re
from datetime import datetime

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bostonchambermusic.org/'
SOURCE = 'Boston Chamber Music Society'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_visible_date(value):
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2},\s+\d{4}',
        clean_text(value),
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_visible_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])M\b', clean_text(value), re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def api_pages(session, endpoint):
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{endpoint}',
            params={'per_page': 100, 'page': page},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f'Unexpected WordPress response for {endpoint}')
        yield from payload
        if len(payload) < 100:
            break
        page += 1


def season_occurrences(pages):
    occurrences = {}
    for page in pages:
        slug = str(page.get('slug') or '')
        if not slug.startswith('season-'):
            continue
        content = (page.get('content') or {}).get('rendered') or ''
        soup = BeautifulSoup(content, 'html.parser')
        for card in soup.select('.entry-item.ignition-event'):
            link = card.select_one('a[href*="/event/"]')
            date = parse_visible_date(card)
            if not link or not date:
                continue
            url = link.get('href', '').split('#', 1)[0]
            venue_node = card.select_one('.entry-item-excerpt, .entry-item-content')
            text = clean_text(card)
            venue_match = re.search(r'\b(Sanders Theatre)\b', text, re.I)
            occurrences[url] = {
                'date': date,
                'time_from': parse_visible_time(card),
                'venue': venue_match.group(1).title() if venue_match else clean_text(venue_node),
            }
    return occurrences


def parse_event(item, occurrence=None):
    title = clean_text((item.get('title') or {}).get('rendered'))
    url = clean_text(item.get('link')).split('#', 1)[0]
    content = (item.get('content') or {}).get('rendered') or ''
    soup = BeautifulSoup(content, 'html.parser')
    calendar = soup.select_one('add-to-calendar-button')

    date = occurrence.get('date') if occurrence else None
    time_from = occurrence.get('time_from') if occurrence else None
    venue = occurrence.get('venue') if occurrence else None
    if calendar:
        date = date or clean_text(calendar.get('startdate'))
        time_from = time_from or clean_text(calendar.get('starttime')) or None
        location = clean_text(calendar.get('location'))
        if not venue and re.search(r'Sanders Theatre', location, re.I):
            venue = 'Sanders Theatre'

    try:
        date = datetime.strptime(date or '', '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None

    # Event posts are BCMS's Cambridge series. The season cards and calendar
    # locations consistently identify Sanders Theatre in Cambridge.
    city = 'Cambridge' if venue else None
    for node in soup.select('script, style, add-to-calendar-button, .wp-block-getwid-post-carousel'):
        node.decompose()
    description = clean_text(soup) or None

    if not all((title, url, date, venue, city)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BostonChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bostonchambermusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session(impersonate='chrome')
        try:
            pages = list(api_pages(session, 'pages'))
            events = list(api_pages(session, 'ignition-event'))
        except (requests.RequestsError, ValueError) as error:
            log_message(
                'Failed to fetch Boston Chamber Music Society API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        overrides = season_occurrences(pages)
        records = []
        for item in events:
            url = clean_text(item.get('link')).split('#', 1)[0]
            record = parse_event(item, overrides.get(url))
            if record:
                records.append(record)
        return sorted(records, key=lambda record: (record['date'], record['time_from'] or '', record['title']))


def main():
    BostonChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
