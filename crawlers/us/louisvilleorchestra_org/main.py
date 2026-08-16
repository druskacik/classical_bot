import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.louisvilleorchestra.org/'
EVENTS_URL = f'{SOURCE_URL}events'
LIST_API = f'{SOURCE_URL}multicategory/category_json'
SOURCE = 'Louisville Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The event pages expose venue names but not a separate city field. These
# mappings cover the orchestra's recurring halls and the multi-venue Messiah.
VENUE_CITIES = {
    'Whitney Hall at The Kentucky Center': 'Louisville',
    'The Brown Theatre': 'Louisville',
    'Iroquois Amphitheater': 'Louisville',
    'Cathedral of the Assumption': 'Louisville',
    'St. Michael Catholic Church (Sing-Along)': 'Louisville',
    'Ogle Center at IU Southeast': 'New Albany',
}

TOUR_CITIES = {
    'Bardstown', 'Cadiz', 'Elizabethtown', 'Frenchburg', 'Greenville',
    'Henderson',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def listing_urls(session):
    params = {
        'category': 0,
        'venue': 0,
        'team': 0,
        'exclude': '',
        'per_page': 12,
        'came_from_page': 'event-list-page',
    }
    urls = []
    offset = 0
    while True:
        # Carbonhouse returns a JSON string containing an HTML fragment. The
        # path number is an item offset, and the endpoint caps pages at 12.
        fragment = get(session, f'{LIST_API}/{offset}', params=params).json()
        soup = BeautifulSoup(fragment, 'html.parser')
        page_urls = [
            link.get('href') for link in soup.select('h3.title a[href]')
            if '/events/detail/' in link.get('href', '')
        ]
        urls.extend(page_urls)
        if len(page_urls) < params['per_page']:
            break
        offset += len(page_urls)
    return list(dict.fromkeys(urls))


def normalized_venue(value):
    venue = clean_text(value)
    # Several venue fields append a street address to the actual venue name.
    return re.sub(r'\s+\d{2,5}\s+(?:West |South )?[A-Z][\w. -]+$', '', venue).strip()


def showing_venue(showing, default_venue):
    text = clean_text(showing.select_one('.showings_date'))
    for venue in VENUE_CITIES:
        if venue in text:
            return venue
    return default_venue


def resolve_city(title, venue):
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    if title.startswith('In Harmony Tour:'):
        city = clean_text(title.split(':', 1)[1])
        if city in TOUR_CITIES:
            return city
    return ''


def showing_datetime(showing):
    date_text = clean_text(showing.select_one('.showings_date'))
    time_text = clean_text(showing.select_one('.time'))
    ticket = showing.select_one('a.tickets')
    ticket_title = clean_text(ticket.get('title')) if ticket else ''
    year_match = re.search(r'\b(20\d{2})\b', ticket_title)
    date_match = re.search(
        r'\b(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{1,2})\b',
        date_text,
    )
    if not year_match or not date_match:
        return None
    try:
        event_date = datetime.strptime(
            f'{date_match.group(1)} {date_match.group(2)} {year_match.group(1)}',
            '%B %d %Y',
        ).date().isoformat()
        time_from = datetime.strptime(time_text, '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None
    return event_date, time_from


def description(soup):
    parts = []
    tagline = clean_text(soup.select_one('.event_heading .tagline'))
    if tagline:
        parts.append(tagline)

    program = soup.select_one('.program_wrapper')
    if program:
        program_copy = BeautifulSoup(str(program), 'html.parser')
        for unwanted in program_copy.select('svg, .audio_icon, .program_link'):
            unwanted.decompose()
        program_text = clean_text(program_copy)
        if program_text:
            parts.append(program_text)

    additional = soup.select_one('.faq.content_item.edp.description .faq_answer')
    if additional:
        additional_text = clean_text(additional)
        if additional_text:
            parts.append(additional_text)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def detail_records(session, url):
    soup = BeautifulSoup(get(session, url).text, 'html.parser')
    title = clean_text(soup.select_one('h1.title'))
    venue_node = soup.select_one('.sidebar_event_venue span')
    default_venue = normalized_venue(venue_node)
    details = description(soup)
    records = []

    for showing in soup.select('.event_showings .listItem'):
        date_time = showing_datetime(showing)
        venue = showing_venue(showing, default_venue)
        city = resolve_city(title, venue)
        if not title or not date_time or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': date_time[0],
            'url': url,
            'time_from': date_time[1],
            'venue': venue,
            'city': city,
            'description': details,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_records, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Louisville Orchestra event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class LouisvilleOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='louisvilleorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    LouisvilleOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
