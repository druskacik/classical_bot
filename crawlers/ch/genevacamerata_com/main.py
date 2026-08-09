import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://genevacamerata.com/fr'
SITEMAP_URL = 'https://genevacamerata.com/sitemap.xml'
SOURCE = 'Geneva Camerata'

HEADERS = {
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11,
    'décembre': 12, 'decembre': 12,
}
DATE_RE = re.compile(
    r'\b(\d{1,2})(?:er)?\s+(' + '|'.join(MONTHS) +
    r')\s+(20\d{2})\s*/\s*([01]?\d|2[0-3]):([0-5]\d)\b',
    re.I,
)
COUNTRIES = {
    'suisse': 'CH', 'france': 'FR', 'allemagne': 'DE', 'mexique': 'MX',
    'chili': 'CL', 'colombie': 'CO', 'norvège': 'NO', 'norvege': 'NO',
    'pays-bas': 'NL', 'italie': 'IT', 'espagne': 'ES', 'autriche': 'AT',
    'royaume-uni': 'GB', 'états-unis': 'US', 'etats-unis': 'US',
}
GENEVA_VENUES = {
    'Bâtiment des Forces Motrices': 'Genève',
    'La Gravière': 'Genève',
    'Théâtre de Carouge': 'Carouge',
    'Théâtre du Loup': 'Genève',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, xml=False):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'xml' if xml else 'html.parser')


def concert_urls(session):
    sitemap = get_soup(session, SITEMAP_URL, xml=True)
    urls = []
    for node in sitemap.select('loc'):
        url = clean_text(node)
        path = urlparse(url).path.rstrip('/')
        if path.startswith('/fr/concerts/') and path.count('/') == 3:
            urls.append(url)
    return sorted(set(urls))


def parse_dates(soup):
    dates = []
    for node in soup.select('.concert-date .datetime, .concert-date'):
        for match in DATE_RE.finditer(clean_text(node)):
            day, month_name, year, hour, minute = match.groups()
            try:
                event_date = date(
                    int(year), MONTHS[month_name.lower()], int(day)
                ).isoformat()
            except ValueError:
                continue
            dates.append((event_date, f'{int(hour):02d}:{minute}'))
    return sorted(set(dates))


def parse_location(soup):
    value = clean_text(soup.select_one('.field--name-field-lieu'))
    if not value or '·' in value:
        # Tour overview pages list countries, rather than a venue for each date.
        return None

    parts = [part.strip() for part in re.split(r'\s+-\s+', value) if part.strip()]
    if len(parts) == 1 and parts[0] in GENEVA_VENUES:
        return parts[0], GENEVA_VENUES[parts[0]], 'CH'
    if len(parts) < 2:
        return None
    country_code = COUNTRIES.get(parts[-1].lower())
    if not country_code:
        return None

    venue = parts[0]
    city = parts[-2] if len(parts) >= 3 else GENEVA_VENUES.get(venue)
    if not venue or not city:
        return None
    return venue, city, country_code


def description(soup):
    parts = []
    for wrapper in soup.select('.group-wrapper'):
        heading = clean_text(wrapper.select_one('.group-title')).lower()
        if heading in {'votre concert', 'programme'}:
            text = clean_text(wrapper.select_one('.group-rows'))
            if text:
                parts.append(text)
    return '\n\n'.join(parts) or None


def make_records(url, soup):
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('h1'))).strip()
    location = parse_location(soup)
    performances = parse_dates(soup)
    if not title or not location or not performances:
        return []

    venue, city, country_code = location
    details = description(soup)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': details,
        }
        for event_date, event_time in performances
    ]


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(make_records(url, future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Geneva Camerata concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue'], record['city']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['title'], record['venue']),
    )


class GenevaCamerataComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='genevacamerata_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    GenevaCamerataComCrawler().run()


if __name__ == '__main__':
    main()
