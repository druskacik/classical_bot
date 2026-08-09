import re
from collections import deque
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.menuhin.ch/de'
SOURCE = 'Menuhin Festival Gstaad'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
}

# Concert pages normally include the locality in the venue label. Eggli and
# the festival's hiking meeting point are the two recurring Gstaad exceptions.
VENUE_CITIES = {
    'Eggli': 'Gstaad',
    "Ausgangspunkt President's Hike": 'Gstaad',
}
KNOWN_CITIES = (
    'Gstaad', 'Saanen', 'Zweisimmen', 'Lauenen', 'Gsteig',
    'Rougemont', 'Rossinière', 'Schönried',
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_date(value):
    match = re.search(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(20\d{2})', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time_and_venue(elements):
    # Some Mountain Spirit pages first advertise a dinner and then the actual
    # concert. The final dated location is the performance represented by the page.
    for element in reversed(elements):
        value = clean_text(element)
        match = re.match(r'^(\d{1,2}):(\d{2}),\s*(.+)$', value)
        if match:
            return f'{int(match.group(1)):02d}:{match.group(2)}', match.group(3).strip()
    return None, ''


def city_from_venue(venue):
    for marker, city in VENUE_CITIES.items():
        if marker.lower() in venue.lower():
            return city
    for city in KNOWN_CITIES:
        if re.search(rf'\b{re.escape(city)}\b', venue, re.I):
            return city
    return ''


def event_description(article):
    parts = []
    for selector in (
        '.event__part--description',
        '.event__artists',
        '.event__program',
    ):
        text = clean_text(article.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.event--detail')
    if not article:
        return None

    title = clean_text(article.select_one('.event__part--title'))
    event_date = parse_date(clean_text(article.select_one('.event__part--date')))
    time_from, venue = parse_time_and_venue(article.select('.event__part--time'))
    city = city_from_venue(venue)
    if not title or not event_date or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(url),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CH',
        'description': event_description(article),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def find_program_url(soup):
    candidates = []
    for anchor in soup.select('a[href]'):
        match = re.search(
            r'/programm-and-tickets/programm-(20\d{2})/?(?:[?#].*)?$',
            anchor['href'],
        )
        if match:
            candidates.append((int(match.group(1)), canonical_url(urljoin(SOURCE_URL, anchor['href']))))
    return max(candidates)[1] if candidates else None


def concert_links(soup, base_url, season_year):
    return {
        canonical_url(urljoin(base_url, anchor['href']))
        for anchor in soup.select('a[href*="/concert/"][href]')
        if f'/{season_year}/' in urljoin(base_url, anchor['href'])
    }


class MenuhinChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='menuhin_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        response = session.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        program_url = find_program_url(BeautifulSoup(response.text, 'html.parser'))
        if not program_url:
            raise ValueError('Could not locate the current Menuhin programme page')

        season_match = re.search(r'/programm-(20\d{2})/?$', program_url)
        if not season_match:
            raise ValueError(f'Could not determine programme year from {program_url}')
        season_year = season_match.group(1)
        response = session.get(program_url, headers=HEADERS, timeout=45)
        response.raise_for_status()

        queue = deque(sorted(concert_links(
            BeautifulSoup(response.text, 'html.parser'), program_url, season_year
        )))
        seen = set()
        records = []
        while queue:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                detail = session.get(url, headers=HEADERS, timeout=45)
                detail.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Menuhin concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            soup = BeautifulSoup(detail.text, 'html.parser')
            for linked_url in concert_links(soup, url, season_year):
                if linked_url not in seen:
                    queue.append(linked_url)

            record = parse_event(detail.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Menuhin concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    MenuhinChCrawler().run()


if __name__ == '__main__':
    main()
