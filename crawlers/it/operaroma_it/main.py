import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaroma.it/'
SEASON_URL = urljoin(SOURCE_URL, 'stagione/')
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php?action=stagione&lang=it')
SOURCE = "Teatro dell'Opera di Roma"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# The theatre also publishes touring dates.  Only locations for which the
# source itself makes the city unambiguous are defaulted to Rome.
ROME_VENUES = {
    'teatro costanzi', 'teatro dell’opera di roma', "teatro dell'opera di roma",
    'teatro nazionale', 'circo massimo', 'terme di caracalla',
    'auditorium parco della musica ennio morricone', 'auditorium conciliazione',
    'la nuvola', 'museo nazionale romano - palazzo altemps',
}

MONTHS = {
    'gen': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mag': 5, 'giu': 6,
    'lug': 7, 'ago': 8, 'set': 9, 'ott': 10, 'nov': 11, 'dic': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def published_seasons(soup):
    seasons = []
    for link in soup.select('#seasons a[href*="#stagione-"]'):
        match = re.search(r'#stagione-(\d{4}-\d{4})', link.get('href', ''))
        if match and match.group(1) not in seasons:
            seasons.append(match.group(1))
    return seasons


def event_city(venue):
    normalized = venue.casefold().strip()
    if normalized in ROME_VENUES or 'roma' in normalized:
        return 'Roma', 'IT'
    # Touring venues commonly include their city after a comma or dash.
    match = re.search(r'(?:,|\s[-–—]\s)\s*([^,–—]+)$', venue)
    if match:
        city = match.group(1).strip()
        if city and not re.search(r'\b(teatro|sala|auditorium|arena)\b', city, re.I):
            return city, 'IT'
    return None


def parse_occurrence(item, year, descriptions):
    title_node = item.select_one('a.title')
    venue_node = item.select_one('.location')
    day_node = item.select_one('.data .day')
    month_node = item.select_one('.data .month')
    if not all((title_node, venue_node, day_node, month_node)):
        return None

    title = clean_text(title_node)
    venue = clean_text(venue_node)
    url = urljoin(SOURCE_URL, title_node.get('href', ''))
    location = event_city(venue)
    try:
        event_date = date(year, MONTHS[clean_text(month_node).casefold()[:3]], int(clean_text(day_node)))
    except (KeyError, TypeError, ValueError):
        return None
    if not title or not url or not venue or not location:
        return None

    time_text = clean_text(item.select_one('.hours'))
    time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', time_text)
    city, country_code = location
    return {
        'title': title,
        'date': event_date.isoformat(),
        'url': url,
        'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': descriptions.get(url),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OperaromaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaroma_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        try:
            page = get_soup(session, SEASON_URL)
            seasons = published_seasons(page)
            if not seasons:
                raise ValueError('No published seasons found')

            season_documents = []
            detail_urls = set()
            for season in seasons:
                response = session.post(
                    AJAX_URL,
                    data={
                        'viewtype': 'calendario', 'location': 'tutti',
                        'genre': 'tutti', 's': '', 'mesi': 'tutti',
                        'season': season, 'ts': '1',
                    },
                    timeout=60,
                )
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')
                season_documents.append((season, soup))
                detail_urls.update(
                    urljoin(SOURCE_URL, node.get('href', ''))
                    for node in soup.select('li.spettacolo-calendario a.title[href]')
                )

            descriptions = {}
            for url in sorted(detail_urls):
                try:
                    detail = get_soup(session, url)
                    content = detail.select_one('#content')
                    if content:
                        for unwanted in content.select(
                            '.breadcrumbs, .buttons, script, style, nav, .social-share'
                        ):
                            unwanted.decompose()
                    descriptions[url] = clean_text(content) or None
                except requests.RequestException as error:
                    descriptions[url] = None
                    log_message(
                        'Failed to fetch Opera Roma event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Opera Roma calendar',
                event='crawler_fetch_failed', level='error', url=SEASON_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        for season, soup in season_documents:
            for item in soup.select('li.spettacolo-calendario'):
                heading = item.find_previous_sibling('li', class_='month')
                year_match = re.search(r'\b(20\d{2})\b', clean_text(heading))
                if not year_match:
                    log_message(
                        'Skipping Opera Roma occurrence without a calendar year',
                        event='crawler_item_skipped', level='warning',
                        url=SEASON_URL, season=season,
                    )
                    continue
                year = int(year_match.group(1))
                record = parse_occurrence(item, year, descriptions)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    OperaromaItCrawler().run()


if __name__ == '__main__':
    main()
