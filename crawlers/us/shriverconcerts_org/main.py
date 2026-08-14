import json
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.shriverconcerts.org/'
SOURCE = 'Shriver Hall Concert Series'
CONCERTS_URL = urljoin(SOURCE_URL, 'concert/')
HISTORY_URL = urljoin(SOURCE_URL, 'about-us/concert-history')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The archive supplies venue names rather than addresses. These are the cities of
# the named venues; unknown venues are deliberately skipped rather than guessed.
VENUE_CITIES = {
    'Shriver Hall': 'Baltimore',
    'Baltimore Museum of Art': 'Baltimore',
    'University of Maryland Baltimore County': 'Catonsville',
    'Towson University': 'Towson',
    'Goucher College': 'Towson',
    'Baltimore Hebrew Congregation': 'Pikesville',
    'Gordon Center for the Performing Arts': 'Owings Mills',
    'The George Washington Carver Center for Arts and Technology': 'Towson',
}


def clean_text(node):
    if node is None:
        return ''
    return ' '.join(node.get_text(' ', strip=True).split())


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%B %d, %Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def music_event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'MusicEvent':
                return candidate
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    data = music_event_data(soup)
    if not data:
        return None

    start = data.get('startDate', '')
    try:
        start_at = datetime.fromisoformat(start.replace('Z', '+00:00'))
        if start_at.tzinfo is not None:
            start_at = start_at.astimezone(ZoneInfo('America/New_York'))
    except (TypeError, ValueError):
        return None

    location = data.get('location') or {}
    address = location.get('address') or {}
    title = str(data.get('name') or '').strip()
    venue = str(location.get('name') or '').strip()
    city = str(address.get('addressLocality') or '').strip() or VENUE_CITIES.get(venue)

    description_parts = []
    overview = soup.select_one('#event_overview .lead')
    if overview:
        description_parts.append(clean_text(overview))
    for piece in soup.select('#event_program .musical_piece'):
        composer = clean_text(piece.find('h2'))
        work = clean_text(piece.find('h3'))
        line = ' — '.join(part for part in (composer, work) if part)
        if line and 'subject to change' not in line.lower():
            description_parts.append(line)

    if not all((title, venue, city)):
        return None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n'.join(dict.fromkeys(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for card in soup.select('.listing, article, main'):
        for link in card.select('a[href]'):
            href = urljoin(CONCERTS_URL, link['href']).split('#', 1)[0]
            if href.startswith(SOURCE_URL) and '/concert/tickets' not in href:
                if link.find_parent(class_='listing') or 'learn more' in clean_text(link).lower():
                    urls.append(href)
    # Current templates do not consistently label the image/title links.
    for link in soup.select('a[href]'):
        href = urljoin(CONCERTS_URL, link['href']).split('#', 1)[0]
        if link.find('img') and href.startswith(SOURCE_URL) and '/site/' not in href:
            urls.append(href)
    return list(dict.fromkeys(urls))


def parse_history(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for listing in soup.select('#performance-archive .listing'):
        cells = listing.find_all('div', recursive=False)
        if len(cells) < 3:
            continue
        event_date = parse_date(clean_text(cells[0]))
        title = clean_text(cells[1])
        venue = clean_text(cells[2])
        city = VENUE_CITIES.get(venue)
        # Streaming-only archive entries are outside project scope.
        if not all((event_date, title, venue, city)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': HISTORY_URL,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class ShriverConcertsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='shriverconcerts_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        try:
            concerts_response = session.get(CONCERTS_URL, timeout=45)
            concerts_response.raise_for_status()
            history_response = session.get(HISTORY_URL, timeout=45)
            history_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Shriver concert indexes',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        for url in detail_urls(concerts_response.text):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_detail(response.text, response.url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Shriver concert detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        current_keys = {(record['date'], record['title'], record['venue']) for record in records}
        records.extend(
            record for record in parse_history(history_response.text)
            if (record['date'], record['title'], record['venue']) not in current_keys
        )
        return records


def main():
    return ShriverConcertsOrgCrawler().run()


if __name__ == '__main__':
    main()
