import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pacificsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'get-tickets')
SOURCE = 'Pacific Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Great Park Live': 'Irvine',
    'Great Park Live, Irvine': 'Irvine',
    'Renée and Henry Segerstrom Concert Hall': 'Costa Mesa',
    'Samueli Theater': 'Costa Mesa',
    'Segerstrom Hall': 'Costa Mesa',
    'Soka Performing Arts Center': 'Aliso Viejo',
}

DATE_TIME_RE = re.compile(
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})(?:\s*\|\s*([0-9:]+\s*[AP]M))?'
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    time_from = None
    if match.group(2):
        try:
            time_from = datetime.strptime(match.group(2).replace(' ', ''), '%I:%M%p').strftime('%H:%M')
        except ValueError:
            pass
    return event_date, time_from


def listing_entries(html):
    soup = BeautifulSoup(html, 'html.parser')
    entries = []
    seen = set()
    for card in soup.select('.show-item'):
        link = card.select_one('a[href*="/show-details/"]')
        title_node = card.select_one('h3')
        if not link or not title_node:
            continue
        url = urljoin(LISTING_URL, link.get('href', ''))
        title = clean_text(title_node)
        if not title or url in seen:
            continue
        seen.add(url)
        venue_node = card.select_one('.show-tags a[href*="venue-details"]')
        entries.append({
            'title': title,
            'url': url,
            'listing_venue': clean_text(venue_node),
        })
    return entries


def location_from_detail(soup, listing_venue):
    venue = listing_venue
    city = VENUE_CITIES.get(venue, '')

    location = soup.select_one('.cd-top-info-desc .media_center_description')
    lines = [line for line in clean_text(location).splitlines() if line]
    if not venue and lines:
        venue = lines[0]

    for line in lines:
        city_match = re.search(r'^\s*([A-Za-z .\'-]+),\s*CA(?:\s+\d{5})?\b', line)
        if city_match:
            city = city_match.group(1).strip(' ,')
            break

    if not city and ',' in venue:
        possible_city = venue.rsplit(',', 1)[1].strip()
        if possible_city and not re.search(r'\d', possible_city):
            city = possible_city

    return venue, city


def parse_detail(entry, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1')) or entry['title']
    venue, city = location_from_detail(soup, entry['listing_venue'])
    description = clean_text(soup.select_one('.cd-top-info-desc')) or None

    occurrences = {}
    for node in soup.select('.concert-detail-tabs h5'):
        event_date, time_from = parse_date_time(node)
        if event_date:
            occurrences[(event_date, time_from)] = None

    # Some pages expose only one occurrence in the top summary.
    if not occurrences:
        event_date, time_from = parse_date_time(soup.select_one('.cd-top-info-title p'))
        if event_date:
            occurrences[(event_date, time_from)] = None

    if not title or not venue or not city:
        log_message(
            'Skipping event with incomplete required location',
            event='crawler_event_skipped',
            level='warning',
            url=entry['url'],
            venue=venue or None,
            city=city or None,
        )
        return []

    return [{
        'title': title,
        'date': event_date,
        'url': entry['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, time_from in occurrences]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    entries = listing_entries(response.text)

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(session.get, entry['url'], timeout=45): entry for entry in entries}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                detail_response = future.result()
                detail_response.raise_for_status()
                records.extend(parse_detail(entry, detail_response.text))
            except requests.RequestException as error:
                log_message(
                    'Concert detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=entry['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PacificSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pacificsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PacificSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
