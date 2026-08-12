import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concertobudapest.hu/'
SOURCE = 'Concerto Budapest'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerts/concert-calendar')
TOURS_URL = urljoin(SOURCE_URL, 'concerts/tours')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The calendar supplies venue names but not cities. These are the venues the
# source currently exposes in its first-party location filter.
VENUE_CITIES = {
    'budapest music center': ('Budapest', 'HU'),
    'concerto music house': ('Budapest', 'HU'),
    'corinthia hotel budapest ballroom': ('Budapest', 'HU'),
    'courtyard of pest county hall': ('Budapest', 'HU'),
    'house of music hungary': ('Budapest', 'HU'),
    'the house of music hungary': ('Budapest', 'HU'),
    'italian cultural institute': ('Budapest', 'HU'),
    'liszt academy': ('Budapest', 'HU'),
    'liszt academy, grand hall': ('Budapest', 'HU'),
    'liszt academy cupola hall': ('Budapest', 'HU'),
    'liszt academy solti hall': ('Budapest', 'HU'),
    'millenáris': ('Budapest', 'HU'),
    'mom cultural center': ('Budapest', 'HU'),
    'müpa budapest': ('Budapest', 'HU'),
    'palace of arts': ('Budapest', 'HU'),
    'pesti vigadó': ('Budapest', 'HU'),
    'várkert bazár': ('Budapest', 'HU'),
    'ferenc liszt conference and cultural center, sopron': ('Sopron', 'HU'),
    'pannonhalma': ('Pannonhalma', 'HU'),
    'vértes agorája - tatabánya': ('Tatabánya', 'HU'),
    'vörösmarty theater székesfehérvár': ('Székesfehérvár', 'HU'),
    'münchen': ('Munich', 'DE'),
}

TOUR_CITIES = {
    'birmingham': ('Birmingham', 'GB'), 'cheltenham': ('Cheltenham', 'GB'),
    'coventry': ('Coventry', 'GB'), 'dublin': ('Dublin', 'IE'),
    'edinburgh': ('Edinburgh', 'GB'), 'glasgow': ('Glasgow', 'GB'),
    'guildford': ('Guildford', 'GB'), 'london': ('London', 'GB'),
    'manchester': ('Manchester', 'GB'), 'munich': ('Munich', 'DE'),
    'münchen': ('Munich', 'DE'), 'sopron': ('Sopron', 'HU'),
    'székesfehérvár': ('Székesfehérvár', 'HU'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def listing_urls(session, feed_url):
    urls = set()
    for page in range(1, 100):
        soup = BeautifulSoup(
            get_response(session, feed_url, params={'page': page}).content,
            'html.parser',
        )
        page_urls = {
            urljoin(SOURCE_URL, node.get('href'))
            for node in soup.select('main a[href^="/v/"]')
            if node.get('href')
        }
        new_urls = page_urls - urls
        urls.update(page_urls)
        if not new_urls or not soup.select_one(f'a[href*="page={page + 1}"]'):
            break
    return urls


def parse_location(venue, title, description):
    normalized = venue.casefold().strip(' .,')
    if normalized in VENUE_CITIES:
        city, country_code = VENUE_CITIES[normalized]
        return venue, city, country_code

    # Prefer the title and venue. Tour descriptions often enumerate every city
    # on the itinerary, which is not evidence for this individual occurrence.
    primary_evidence = f'{title}\n{venue}'.casefold()
    for evidence in (primary_evidence, description.casefold()):
        for name, (city, country_code) in TOUR_CITIES.items():
            if re.search(rf'\b{re.escape(name)}\b', evidence):
                return venue, city, country_code
    return None


def parse_event(session, url):
    soup = BeautifulSoup(get_response(session, url).content, 'html.parser')
    article = soup.select_one('main article.article_type-program')
    if not article:
        return None

    title = clean_text(article.select_one('h1'))
    time_node = article.select_one('time.article__start-date[datetime]')
    if not title or not time_node:
        return None
    try:
        event_date = date.fromisoformat(time_node.get('datetime')).isoformat()
    except (TypeError, ValueError):
        return None

    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(time_node))
    time_from = time_match.group(0) if time_match else None
    lead = clean_text(article.select_one('.article__lead'))
    body = clean_text(article.select_one('.article__body'))
    description = '\n\n'.join(part for part in (lead, body) if part) or None

    venue = clean_text(article.select_one('.article__loc'))
    if not venue and lead:
        match = re.search(r'(?im)^venue:\s*([^\n]+)', lead)
        if match:
            venue = match.group(1).strip()
    if not venue:
        # Older tour pages commonly encode "Hall, City" only in the title.
        match = re.search(
            r'(?i)\b([^\n–—]+(?:hall|centre|center|academy|theatre|theater)),\s*'
            r'([\wÀ-ž .-]+)', title,
        )
        if match:
            venue = match.group(1).strip(' -')
            venue = re.sub(r'(?i)^concerto budapest\s*[-–—]?\s*', '', venue)
    if not venue:
        return None

    location = parse_location(venue, title, description or '')
    if not location:
        return None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ConcertoBudapestHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concertobudapest_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = set()
        for feed_url in (CALENDAR_URL, TOURS_URL):
            try:
                urls.update(listing_urls(session, feed_url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Concerto Budapest listing',
                    event='crawler_page_failed', level='warning', url=feed_url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(parse_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Concerto Budapest concert',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    ConcertoBudapestHuCrawler().run()


if __name__ == '__main__':
    main()
