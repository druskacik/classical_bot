import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rcs.ac.uk/'
SEARCH_URL = f'{SOURCE_URL}whats-on-search/'
SOURCE = 'Royal Conservatoire of Scotland'
FILTER_TYPES = ('classical-music', 'opera-and-vocal')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
POSTCODE_RE = re.compile(r'^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$', re.IGNORECASE)
EXACT_DATE_RE = re.compile(
    r'^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+'
    r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+(20\d{2})$',
    re.IGNORECASE,
)


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def listing_urls(session):
    urls = []
    for filter_type in FILTER_TYPES:
        soup = BeautifulSoup(
            get_response(session, SEARCH_URL, {'type': filter_type}).content,
            'html.parser',
        )
        for link in soup.select('.c-event-card__permalink[href]'):
            url = link.get('href', '').split('#', 1)[0]
            if re.match(r'^https://www\.rcs\.ac\.uk/whats-on/[^/]+/?$', url):
                urls.append(url)
    return list(dict.fromkeys(urls))


def city_from_map_link(link):
    if not link:
        return None
    values = parse_qs(urlparse(link.get('href', '')).query).get('q', [])
    if not values:
        return None
    parts = [part.strip() for part in re.split(r'[,\n]+', unquote(values[0])) if part.strip()]
    for index, part in enumerate(parts):
        if POSTCODE_RE.fullmatch(part) and index:
            return parts[index - 1]
    return None


def venue_details(soup):
    details = []
    for node in soup.select('.c-event__details-venue > span'):
        link = node.select_one('a')
        venue = clean_text(link or node)
        city = city_from_map_link(link)
        if venue:
            details.append((venue, city))
    return details


def location_from_note(note, venues):
    text = clean_text(note)
    if ',' in text:
        venue, city = (part.strip() for part in text.rsplit(',', 1))
        if venue and city and not any(char.isdigit() for char in city):
            return venue, city
    if len(venues) == 1:
        return venues[0]
    return None, None


def parse_date(text):
    match = EXACT_DATE_RE.fullmatch(clean_text(text))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('.c-masthead__title'))
    if not title:
        return []

    description = clean_text(soup.select_one('.event__important-information')) or None
    venues = venue_details(soup)
    occurrences = []

    for group in soup.select('.c-instances__group'):
        date_node = group.select_one('time[datetime]')
        if not date_node:
            continue
        try:
            event_date = datetime.fromisoformat(date_node.get('datetime')).date().isoformat()
        except (TypeError, ValueError):
            continue
        for item in group.select('.c-instances__list-item'):
            time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(item.select_one('.c-instances__list-time')))
            time_from = time_match.group(0) if time_match else None
            venue, city = location_from_note(item.select_one('.c-instances__list-note'), venues)
            if venue and city:
                occurrences.append((event_date, time_from, venue, city))

    if not occurrences:
        event_date = parse_date(soup.select_one('.c-masthead__daterange'))
        time_match = re.search(
            r'\b([01]\d|2[0-3]):[0-5]\d\b',
            clean_text(soup.select_one('.c-masthead__time')),
        )
        if event_date and len(venues) == 1 and venues[0][1]:
            occurrences.append((event_date, time_match.group(0) if time_match else None, *venues[0]))

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from, venue, city in occurrences
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(future.result().content, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape RCS event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class RcsAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rcs_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RcsAcUkCrawler().run()


if __name__ == '__main__':
    main()
