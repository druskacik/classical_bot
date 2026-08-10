import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.drp-orchester.de/drp/index.html'
SOURCE = 'Deutsche Radio Philharmonie'
BASE_URL = 'https://www.drp-orchester.de/'
TIMELINE_URL = BASE_URL + 'drp/timeline{season}_uebersicht100.json'

HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

PATH_LOCATIONS = {
    'saarbruecken': ('Saarbrücken', 'DE'),
    'kaiserslautern': ('Kaiserslautern', 'DE'),
    'forbach': ('Forbach', 'FR'),
}

# Tour destinations represented in the published season feeds. Using the URL
# token avoids mistaking festival names or concert-series prose for a city.
TOUR_LOCATIONS = {
    'alicante': ('Alicante', 'ES'),
    'baden-baden': ('Baden-Baden', 'DE'),
    'barcelona': ('Barcelona', 'ES'),
    'brugg': ('Brugg', 'CH'),
    'dillingen': ('Dillingen/Saar', 'DE'),
    'koblenz': ('Koblenz', 'DE'),
    'landau': ('Landau in der Pfalz', 'DE'),
    'ludwigsburg': ('Ludwigsburg', 'DE'),
    'madrid': ('Madrid', 'ES'),
    'mainz': ('Mainz', 'DE'),
    'mannheim': ('Mannheim', 'DE'),
    'metz': ('Metz', 'FR'),
    'rehlingen': ('Rehlingen-Siersburg', 'DE'),
    'saarlouis': ('Saarlouis', 'DE'),
    'klassik_am_see': ('Losheim am See', 'DE'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_ids():
    # Old feeds remain public when the site still retains an archive item.
    # Include the upcoming season around the calendar-year boundary as well.
    current_year = date.today().year
    return [f'{year % 100:02d}-{(year + 1) % 100:02d}' for year in range(current_year - 8, current_year + 1)]


def fetch_listing(session, season):
    url = TIMELINE_URL.format(season=season)
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json().get('timeline', {}).get('date', [])


def resolve_location(url):
    path = urlparse(url).path.casefold()
    for token, location in PATH_LOCATIONS.items():
        if f'/{token}/' in path:
            return location
    filename = path.rsplit('/', 1)[-1]
    if re.search(r'_sb(?:100)?\.html$', filename):
        return 'Saarbrücken', 'DE'
    if re.search(r'_kl(?:100)?\.html$', filename):
        return 'Kaiserslautern', 'DE'
    for token, location in TOUR_LOCATIONS.items():
        if token in filename:
            return location
    return None


def parse_header(value):
    parts = [clean_text(part) for part in (value or '').split('|')]
    if len(parts) < 3:
        return None, None
    time_match = re.search(r'(\d{1,2})(?:[.:](\d{2}))?\s*Uhr', parts[1], re.I)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'
    return time_from, parts[2]


def description_from(article):
    paragraphs = []
    for element in article.select('p:not(.article__header__text)'):
        text = clean_text(element)
        if not text or text.startswith('Termin:') or text.startswith('Tickets |'):
            continue
        if text not in paragraphs:
            paragraphs.append(text)
    return '\n\n'.join(paragraphs) or None


def make_record(item, html):
    url = urljoin(BASE_URL, item.get('url') or '')
    location = resolve_location(url)
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('.article')
    title_element = article.find('h1') if article else None
    header_element = article.find('h2') if article else None
    title = clean_text(title_element)
    time_from, venue = parse_header(clean_text(header_element))
    if 'klassik_am_see' in url.casefold() and venue == 'Losheim am See':
        venue = 'Losheimer Stausee'

    try:
        year, month, day = (int(part) for part in (item.get('startDate') or '').split(','))
        event_date = date(year, month, day).isoformat()
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not location:
        return None
    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from(article),
    }


class DrpOrchesterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='drp_orchester_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = {}
        for season in season_ids():
            try:
                for item in fetch_listing(session, season):
                    if item.get('url'):
                        items[urljoin(BASE_URL, item['url'])] = item
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape DRP season feed',
                    event='crawler_page_failed',
                    level='warning',
                    url=TIMELINE_URL.format(season=season),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(session.get, url, timeout=45): (url, item)
                for url, item in items.items()
            }
            for future in as_completed(futures):
                url, item = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = make_record(item, response.text)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape DRP concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    DrpOrchesterDeCrawler().run()


if __name__ == '__main__':
    main()
