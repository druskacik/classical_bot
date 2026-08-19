import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musiconnorwaypond.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'upcoming-events')
SOURCE = 'Music on Norway Pond'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if not element:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(element):
    if not element:
        return None
    value = clean_text(element).upper().replace('.', '')
    try:
        return datetime.strptime(value, '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def venue_from(item, description):
    address = item.select_one('.eventlist-meta-address')
    if not address:
        return None
    map_link = address.select_one('.eventlist-meta-address-maplink')
    if map_link:
        map_link.extract()
    venue = clean_text(address).strip(' ,-')
    if venue.lower() == 'sold out!':
        return 'Private residence' if 'house concert' in description.lower() else None
    if re.search(r'\bPO Box\b', venue, re.I):
        return None
    if venue.lower().startswith('on the hancock common'):
        return 'Hancock Common'
    if re.search(r'\bDepot Road\b', venue, re.I):
        return 'Private venue on Norway Pond'
    return venue or None


def detail_description(session, url, fallback):
    try:
        soup = get_soup(session, url)
        content = soup.select_one('.eventitem-column-content')
        return clean_text(content) or fallback or None
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Music on Norway Pond event detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return fallback or None


def parse_event(session, item):
    link = item.select_one('.eventlist-title-link[href]')
    date_element = item.select_one('time.event-date[datetime]')
    if not link or not date_element:
        return None

    title = clean_text(link)
    event_date = valid_date(date_element.get('datetime'))
    url = urljoin(EVENTS_URL, link.get('href', '').strip())
    fallback = clean_text(item.select_one('.eventlist-excerpt'))
    description = detail_description(session, url, fallback)
    venue = venue_from(item, description or fallback)

    # Every published occurrence is in Hancock. Entries whose location field is
    # only a mailing address are enrollment/rehearsal notices and are skipped.
    if not title or not event_date or not url or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(item.select_one('.event-time-localized-start')),
        'venue': venue,
        'city': 'Hancock',
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, EVENTS_URL)
    records = []
    for item in soup.select('article.eventlist-event'):
        record = parse_event(session, item)
        if record:
            records.append(record)
    unique = {(record['url'], record['date']): record for record in records}
    return sorted(unique.values(), key=lambda record: (record['date'], record['time_from'] or '', record['title']))


class MusicOnNorwayPondCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiconnorwaypond_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        return get_concerts()


def main():
    return MusicOnNorwayPondCrawler().run()


if __name__ == '__main__':
    main()
