import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://yle.fi/aihe/rso'
SOURCE = 'Yle Radion sinfoniaorkesteri'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}
DATE_RE = re.compile(
    r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(?:klo\s*)?'
    r'([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\b',
    re.IGNORECASE,
)

# RSO is based at Musiikkitalo, but regularly tours. These are explicit venue
# clues found in the published season archive; the home-city default is applied
# only to Musiikkitalo rooms.
LOCATION_RULES = (
    (r'\bMusiikkitalo\b|\bPaavo-sali\b', 'Helsinki', 'FI'),
    (r'\bHelsinki\b', 'Helsinki', 'FI'),
    (r'\bTampere\b', 'Tampere', 'FI'),
    (r'\bTurku\b', 'Turku', 'FI'),
    (r'\bLahti\b', 'Lahti', 'FI'),
    (r'\bPorvoo\b', 'Porvoo', 'FI'),
    (r'\bLohja\b', 'Lohja', 'FI'),
    (r'\bBerlin\b', 'Berlin', 'DE'),
    (r'\bHamburg\b|\bElbphilharmonie\b', 'Hamburg', 'DE'),
    (r'\bKöln\b|\bCologne\b', 'Köln', 'DE'),
    (r'\bAmsterdam\b|\bConcertgebouw\b', 'Amsterdam', 'NL'),
    (r'\bLondon\b|\bBarbican\b|\bRoyal Albert Hall\b', 'London', 'GB'),
    (r'\bWien\b|\bVienna\b|\bMusikverein\b', 'Wien', 'AT'),
    (r'\bParis\b', 'Paris', 'FR'),
    (r'\bStockholm\b', 'Stockholm', 'SE'),
    (r'\bTallinn\b', 'Tallinn', 'EE'),
    (r'\bStuttgart\b', 'Stuttgart', 'DE'),
    (r'\bFrankfurt\b', 'Frankfurt', 'DE'),
    (r'\bMoskova\b|\bMoscow\b', 'Moskova', 'RU'),
    (r'\bViipuri\b|\bVyborg\b', 'Viipuri', 'RU'),
    (r'\bPietari\b|\bSt.? Petersburg\b', 'Pietari', 'RU'),
    (r'\bTokio\b|\bTokyo\b', 'Tokio', 'JP'),
    (r'\bTokuyama\b', 'Tokuyama', 'JP'),
    (r'\bOsaka\b', 'Osaka', 'JP'),
    (r'\bInnsbruck\b', 'Innsbruck', 'AT'),
    (r'\bSalzburg\b', 'Salzburg', 'AT'),
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def discover_event_urls(session):
    hub = get_soup(session, SOURCE_URL)
    season_urls = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in hub.select('a[href*="/rso/konsertit-kausi-"]')
    }

    # Current season pages retain the complete first-party archive navigation.
    for season_url in list(season_urls):
        season = get_soup(session, season_url)
        season_urls.update(
            urljoin(season_url, link.get('href'))
            for link in season.select('a[href*="/rso/konsertit-kausi-"]')
        )

    event_urls = set()
    for season_url in sorted(season_urls):
        try:
            season = get_soup(session, season_url)
        except requests.RequestException as error:
            log_message(
                'Failed to read Yle RSO season page',
                event='crawler_item_failed',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        event_urls.update(
            urljoin(season_url, link.get('href')).split('#', 1)[0]
            for link in season.select('a[href*="/aihe/a/"]')
        )
    return sorted(event_urls)


def resolve_location(venue):
    for pattern, city, country_code in LOCATION_RULES:
        if re.search(pattern, venue, re.IGNORECASE):
            return city, country_code
    return None, None


def date_location(article):
    paragraphs = article.select('p')
    for index, paragraph in enumerate(paragraphs):
        text = clean_text(paragraph)
        match = DATE_RE.search(text)
        if not match:
            continue
        try:
            event_date = date(
                int(match.group(3)), int(match.group(2)), int(match.group(1))
            ).isoformat()
        except ValueError:
            continue
        time_from = f'{int(match.group(4)):02d}:{match.group(5) or "00"}'
        remainder = clean_text(text[match.end():]).strip(' ,-–')
        # Broadcast information is sometimes another <br>-separated line in
        # the same paragraph. The venue is always the first line after time.
        venue = next((line.strip(' ,-–') for line in remainder.splitlines() if line.strip()), '')
        if not venue and index + 1 < len(paragraphs):
            venue = clean_text(paragraphs[index + 1]).splitlines()[0].strip(' ,-–')
        if venue:
            return event_date, time_from, venue
    return None, None, None


def parse_event(soup, url):
    article = soup.select_one('main article')
    title = clean_text(article.select_one('h1')) if article else ''
    event_date, time_from, venue = date_location(article) if article else (None, None, None)
    if re.search(r'\bkaikki konsertit\b|\bkonsertit,? kausi\b', title, re.IGNORECASE):
        return None
    if re.search(r'\bliput\b|\bosta\b|\byle (?:radio|areena|tv)\b', venue or '', re.IGNORECASE):
        return None
    city, country_code = resolve_location(f'{venue or ""} {title}')
    if not all((title, event_date, url, venue, city, country_code)):
        return None

    paragraphs = [clean_text(node) for node in article.select('p')]
    description = clean_text('\n\n'.join(text for text in paragraphs if text)) or None
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


def scrape_events():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = discover_event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Yle RSO concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class YleFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yle_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    YleFiCrawler().run()


if __name__ == '__main__':
    main()
