import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.daviddeltredici.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
SOURCE = 'David Del Tredici'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

# Locations in this composer's calendar are free text, usually a venue followed
# by a city. These aliases cover the cities in the site's complete archive and
# allow new entries in the same established venues to remain parseable.
LOCATIONS = [
    (r'Annandale-on-Hudson', 'Annandale-on-Hudson', 'US'),
    (r'Rio de Janeiro', 'Rio de Janeiro', 'BR'),
    (r'Guayaquil', 'Guayaquil', 'EC'),
    (r'Quito', 'Quito', 'EC'),
    (r'Baden-Baden', 'Baden-Baden', 'DE'),
    (r'Luxembourg', 'Luxembourg', 'LU'),
    (r'Eindhoven', 'Eindhoven', 'NL'),
    (r'Gateshead', 'Gateshead', 'GB'),
    (r'Prades', 'Prades', 'FR'),
    (r'Milan|Milano', 'Milan', 'IT'),
    (r'Berlin', 'Berlin', 'DE'),
    (r'Paris', 'Paris', 'FR'),
    (r'Saratoga Springs', 'Saratoga Springs', 'US'),
    (r'San Francisco', 'San Francisco', 'US'),
    (r'New York City|New York,?\s*(?:NY)?|\bNYC\b', 'New York', 'US'),
    (r'Williamsburg|Park Slope|Brooklyn', 'Brooklyn', 'US'),
    (r'Pittsburgh', 'Pittsburgh', 'US'),
    (r'Pittsfield', 'Pittsfield', 'US'),
    (r'Portland', 'Portland', 'US'),
    (r'Miami', 'Miami', 'US'),
    (r'Boston', 'Boston', 'US'),
    (r'Detroit', 'Detroit', 'US'),
    (r'Ann Arbor', 'Ann Arbor', 'US'),
    (r'Montgomery', 'Montgomery', 'US'),
    (r'Mount Vernon', 'Mount Vernon', 'US'),
    (r'Forest City', 'Forest City', 'US'),
    (r'Princeton', 'Princeton', 'US'),
    (r'Tampa', 'Tampa', 'US'),
    (r'Bronxville', 'Bronxville', 'US'),
    (r'Albany', 'Albany', 'US'),
    (r'Milwaukee', 'Milwaukee', 'US'),
    (r'Montclair', 'Montclair', 'US'),
    (r'La Jolla', 'La Jolla', 'US'),
    (r'Crested Butte', 'Crested Butte', 'US'),
]

VENUE_CITIES = [
    (r'Heinz Hall', 'Pittsburgh', 'US'),
    (r'Galapagos Art Space', 'Brooklyn', 'US'),
    (r'Baruch Performing Arts Center', 'New York', 'US'),
    (r'Tanglewood Music Festival', 'Lenox', 'US'),
    (r'Orchestra Hall', 'Detroit', 'US'),
    (r'SubCulture', 'New York', 'US'),
    (r'Montclair State University|Alexander Kasser Theater', 'Montclair', 'US'),
    (r'University of Wisconsin-Milwaukee', 'Milwaukee', 'US'),
    (r'CCNY|City College of New York', 'New York', 'US'),
    (r'Cornelia Street Cafe', 'New York', 'US'),
    (r'Guggenheim Museum', 'New York', 'US'),
    (r'Morgan Library', 'New York', 'US'),
    (r'Palace Theater', 'Albany', 'US'),
    (r'Bargemusic|Barge Music', 'Brooklyn', 'US'),
]

VENUE_NAMES = [
    (r'First Lutheran Church of Boston', 'First Lutheran Church of Boston'),
    (r'Conservatorio di Musica Giuseppe Verdi di Milano',
     'Conservatorio di Musica Giuseppe Verdi di Milano'),
    (r'University of Wisconsin-Milwaukee', 'University of Wisconsin-Milwaukee'),
    (r'Sage Gateshead', 'Sage Gateshead'),
]


def clean_text(value):
    text = (value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def location_from_item(item):
    element = item.select_one('.event-location')
    if not element:
        return None
    icon = element.select_one('i')
    if icon:
        icon.decompose()
    raw = clean_text(element.get_text('\n', strip=True))
    title = (item.select_one('.event-title').get_text(' ', strip=True)
             if item.select_one('.event-title') else '')

    city = country_code = None
    city_match = None
    raw_matches = []
    for pattern, candidate_city, candidate_country in LOCATIONS:
        match = re.search(pattern, raw, re.I)
        if match:
            raw_matches.append((match.start(), match, candidate_city, candidate_country))
    if raw_matches:
        _, city_match, city, country_code = min(raw_matches, key=lambda value: value[0])
    if not city:
        for pattern, candidate_city, candidate_country in VENUE_CITIES:
            if re.search(pattern, raw, re.I):
                city, country_code = candidate_city, candidate_country
                break
    if not city:
        for pattern, candidate_city, candidate_country in LOCATIONS:
            if re.search(pattern, title, re.I):
                city, country_code = candidate_city, candidate_country
                break
    if not city:
        return None

    venue_text = raw[:city_match.start()] if city_match else raw
    for pattern, venue_name in VENUE_NAMES:
        if re.search(pattern, raw, re.I):
            venue_text = venue_name
            break
    lines = []
    for line in venue_text.split('\n'):
        line = line.strip(' ,:-')
        # Addresses are useful for finding the city but are not venue names.
        line = re.sub(r'(?:,\s*)?\b\d+[A-Za-z-]*\s+.*$', '', line).strip(' ,:-')
        if line and line.lower() not in {'california', 'colorado'}:
            lines.append(line)
    venue = ', '.join(dict.fromkeys(lines))
    venue = re.sub(r',\s*(?:in|near)\b.*$', '', venue, flags=re.I).strip(' ,')
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def dates_from_item(item):
    element = item.select_one('.event-dates')
    if not element:
        return []
    hidden = element.select_one('.hidden')
    event_id = clean_text(hidden.get_text()) if hidden else ''
    if hidden:
        hidden.decompose()
    text = clean_text(element.get_text(' ', strip=True))
    matches = re.finditer(
        r'\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})'
        r'(?:\s+(\d{1,2}:\d{2})\s*([ap]m))?',
        text,
        re.I,
    )
    results = []
    for match in matches:
        try:
            date = datetime.strptime(match.group(1), '%b %d, %Y').date().isoformat()
        except ValueError:
            continue
        time_from = None
        if match.group(2):
            parsed = datetime.strptime(
                f'{match.group(2)} {match.group(3)}', '%I:%M %p'
            )
            time_from = parsed.strftime('%H:%M')
        results.append((date, time_from, event_id))
    return results


def description_from_item(item):
    parts = []
    for selector, label in (
        ('.event-works', 'Works'),
        ('.event-artists', 'Performers'),
        ('.event-body', None),
    ):
        element = item.select_one(selector)
        value = clean_text(element.get_text('\n', strip=True)) if element else ''
        if value:
            parts.append(f'{label}\n{value}' if label else value)
    return '\n\n'.join(parts) or None


def records_from_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.event-item'):
        title_element = item.select_one('.event-title')
        title = clean_text(title_element.get_text(' ', strip=True)) if title_element else ''
        location = location_from_item(item)
        dates = dates_from_item(item)
        if not title or not location or not dates:
            continue
        venue, city, country_code = location
        description = description_from_item(item)
        for date, time_from, event_id in dates:
            records.append({
                'title': title,
                'date': date,
                'url': f'{page_url}#{event_id}' if event_id else page_url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    year_urls = {
        urljoin(CALENDAR_URL, link['href'])
        for link in soup.select('a[href*="/calendar/year/"]')
        if re.search(r'/calendar/year/\d{4}/?$', link.get('href', ''))
    }

    records = records_from_page(response.text, CALENDAR_URL)
    for page_url in sorted(year_urls):
        try:
            page = session.get(page_url, timeout=45)
            page.raise_for_status()
            records.extend(records_from_page(page.text, page_url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch David Del Tredici calendar archive',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            if not records:
                raise

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['venue']
    ))
    if not result:
        log_message(
            'No valid David Del Tredici calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return result


class DavidDelTrediciComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='daviddeltredici_com',
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
    DavidDelTrediciComCrawler().run()


if __name__ == '__main__':
    main()
