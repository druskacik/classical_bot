import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.doulcememoire.com/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-agenda-1.xml'
SOURCE = 'Doulce Mémoire'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1,
    'jan': 1,
    'février': 2,
    'fevrier': 2,
    'fév': 2,
    'fev': 2,
    'mars': 3,
    'avril': 4,
    'avr': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'juil': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'sept': 9,
    'octobre': 10,
    'oct': 10,
    'novembre': 11,
    'nov': 11,
    'décembre': 12,
    'decembre': 12,
    'déc': 12,
    'dec': 12,
}

COUNTRIES = {
    'allemagne': 'DE',
    'autriche': 'AT',
    'belgique': 'BE',
    'brésil': 'BR',
    'bresil': 'BR',
    'canada': 'CA',
    'chine': 'CN',
    'corée du sud': 'KR',
    'coree du sud': 'KR',
    'espagne': 'ES',
    'états-unis': 'US',
    'etats-unis': 'US',
    'france': 'FR',
    'italie': 'IT',
    'japon': 'JP',
    'luxembourg': 'LU',
    'mexique': 'MX',
    'monaco': 'MC',
    'monténégro': 'ME',
    'montenegro': 'ME',
    'pays-bas': 'NL',
    'portugal': 'PT',
    'royaume-uni': 'GB',
    'singapour': 'SG',
    'suisse': 'CH',
    'taïwan': 'TW',
    'taiwan': 'TW',
    'turquie': 'TR',
}

TIME_RE = re.compile(r'^\s*([01]?\d|2[0-3])[:h]([0-5]\d)\s*[\-\u2013\u2014]\s*(.+)$')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def sitemap_events(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    events = []
    for node in soup.find_all('url'):
        location = node.find('loc')
        modified = node.find('lastmod')
        year_match = re.match(r'(\d{4})-', clean_text(modified))
        if location and year_match:
            events.append((clean_text(location), int(year_match.group(1))))
    return events


def location_details(place):
    normalized = clean_text(place)
    country_code = 'FR'
    for label, code in COUNTRIES.items():
        if re.search(rf'(?<!\w){re.escape(label)}(?!\w)', normalized, re.I):
            country_code = code
            break

    city = re.sub(r'^\s*\d{4,5}\s+', '', normalized)
    city = re.sub(r'^\s*(?:annulé|reporté|cancelled|postponed)\s*[-:]\s*', '', city, flags=re.I)
    city = re.sub(r'\s*\([^()]+\)\s*$', '', city)
    return city.strip(' ,-'), country_code


def description_from_page(soup):
    article = soup.select_one('article.col-xs-12')
    if not article:
        return None
    parts = []
    for node in article.select('.text.main, .blocks:not(.infos)'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def venue_and_time(soup, subtitle, place):
    time_from = None
    venue = clean_text(subtitle)
    match = TIME_RE.match(venue)
    if match:
        time_from = f'{int(match.group(1)):02d}:{match.group(2)}'
        venue = match.group(3).strip()

    if not venue:
        info = soup.select_one('address.infos.hidden-xs') or soup.select_one('address.infos')
        lines = clean_text(info).splitlines() if info else []
        place_key = clean_text(place).casefold()
        venue = next(
            (
                line for line in lines
                if line.lower() != 'infos pratiques' and line.casefold() != place_key
            ),
            '',
        )
    return venue, time_from


def event_record(session, url, year):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title = clean_text(soup.select_one('h1.post-title'))
    details = soup.select_one('.event-details.agenda')
    day_text = clean_text(details.select_one('.day')) if details else ''
    month_text = clean_text(details.select_one('.month')).lower() if details else ''
    place = clean_text(details.select_one('.place-block .post-title')) if details else ''
    subtitle = clean_text(details.select_one('.place-block .sub-title')) if details else ''
    month = MONTHS.get(month_text)
    try:
        event_date = date(year, month, int(day_text)).isoformat()
    except (TypeError, ValueError):
        return None

    city, country_code = location_details(place)
    venue, time_from = venue_and_time(soup, subtitle, place)
    if not title or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from_page(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        events = sitemap_events(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Doulce Mémoire agenda sitemap',
            event='crawler_fetch_failed',
            level='error',
            url=SITEMAP_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(event_record, session, url, year): url
            for url, year in events
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Doulce Mémoire event',
                    event='crawler_event_fetch_failed',
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
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class DoulcememoireComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='doulcememoire_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    DoulcememoireComCrawler().run()


if __name__ == '__main__':
    main()
