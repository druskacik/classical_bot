import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.swr.de/'
SOURCE = 'SWR Eventkalender'
CALENDAR_URL = urljoin(
    SOURCE_URL, 'unternehmen/uebergreifender-veranstaltungskalender-100.html'
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# Explicit foreign tour locations seen in the calendar. Other entries belong to
# SWR's German event area unless their location says otherwise.
FOREIGN_CITIES = {
    'new york': ('New York', 'US'),
    'luzern': ('Luzern', 'CH'),
    'kriens': ('Kriens', 'CH'),
    'basel': ('Basel', 'CH'),
    'zürich': ('Zürich', 'CH'),
    'zurich': ('Zürich', 'CH'),
    'paris': ('Paris', 'FR'),
    'vienna': ('Vienna', 'AT'),
    'wien': ('Wien', 'AT'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def discover_pages(session):
    soup = get_soup(session, CALENDAR_URL)
    pages = {CALENDAR_URL}
    pages.update(
        canonical_url(link['href'])
        for link in soup.select('a[href*="_paged"]')
    )
    return soup, sorted(pages)


def city_and_country(location):
    folded = location.casefold()
    for key, result in FOREIGN_CITIES.items():
        if re.search(rf'(?<!\w){re.escape(key)}(?!\w)', folded):
            return result

    postal = re.search(r'\b\d{5}\s+([A-ZÄÖÜ][\wÄÖÜäöüß.-]*(?:[ -][A-ZÄÖÜ][\wÄÖÜäöüß.-]*){0,3})', location)
    if postal:
        city = re.split(r'[,\n(]', postal.group(1))[0].strip()
        return (city, 'DE') if city else (None, None)

    parts = [part.strip() for part in location.split(',') if part.strip()]
    if len(parts) >= 2:
        # SWR normally formats either "City, Venue" or "Venue, City".
        first, last = parts[0], parts[-1]
        venue_words = re.compile(
            r'(?:arena|halle|saal|studio|museum|kirche|markt|park|festung|forum|'
            r'bühne|theater|club|schloss|hof|zentrum)', re.I
        )
        if venue_words.search(first) and not re.search(r'\d', last):
            return last, 'DE'
        if not re.search(r'\d', first):
            return first, 'DE'
    return None, None


def venue_from(location, city):
    parts = [part.strip() for part in re.split(r'[,\n]', location) if part.strip()]
    useful = []
    for part in parts:
        without_postal = re.sub(r'\b\d{5}\b.*$', '', part).strip()
        if not without_postal or without_postal.casefold() == city.casefold():
            continue
        # Detail pages often expand the concise venue into a multiline postal
        # address. Keep hall/building names, but never store address segments.
        if re.search(r'\d', without_postal) or re.search(
            r'(?:straße|strasse|street|road|avenue|weg)$', without_postal, re.I
        ):
            continue
        useful.append(without_postal)
    return ', '.join(useful) or None


def listing_items(soup):
    items = []
    for node in soup.select('li.event-list-item'):
        time_node = node.select_one('time[datetime]')
        title_node = node.select_one('.event-list-item-data h2')
        location = clean_text(node.select_one('.event-detail-location + dd'))
        if not location:
            location = clean_text(node.select_one('.event-list-item-data .location'))
        if not time_node or not title_node or not location:
            continue
        try:
            start = datetime.fromisoformat(time_node['datetime'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            continue
        city, country_code = city_and_country(location)
        venue = venue_from(location, city) if city else None
        if not city or not venue:
            continue
        detail_link = node.select_one('.event-actions a[href]:not([href^="data:"])')
        url = canonical_url(detail_link['href']) if detail_link else CALENDAR_URL
        items.append({
            'title': clean_text(title_node).replace('\n', ' - '),
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': clean_text(node.select_one('.event-details > p')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return items


def detail_description(session, url, fallback):
    if url == CALENDAR_URL:
        return fallback
    soup = get_soup(session, url)
    body = soup.select_one('.detail-body .bodytext')
    return clean_text(body) or fallback


class SwrDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='swr_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        first_soup, pages = discover_pages(session)
        records = listing_items(first_soup)
        for page in pages:
            if page == CALENDAR_URL:
                continue
            try:
                records.extend(listing_items(get_soup(session, page)))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape SWR calendar page',
                    event='crawler_page_failed',
                    level='warning',
                    url=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(detail_description, session, record['url'], record['description']): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape SWR event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    SwrDeCrawler().run()


if __name__ == '__main__':
    main()
