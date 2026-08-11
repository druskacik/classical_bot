import re
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://leconcertdastree.fr/'
SOURCE = "Le Concert d'Astrée"
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
}

COUNTRY_CODES = {
    'allemagne': 'DE',
    'autriche': 'AT',
    'belgique': 'BE',
    'canada': 'CA',
    'chine': 'CN',
    'danemark': 'DK',
    'espagne': 'ES',
    'états-unis': 'US',
    'etats-unis': 'US',
    'finlande': 'FI',
    'france': 'FR',
    'grèce': 'GR',
    'irlande': 'IE',
    'islande': 'IS',
    'italie': 'IT',
    'japon': 'JP',
    'luxembourg': 'LU',
    'norvège': 'NO',
    'pays-bas': 'NL',
    'pologne': 'PL',
    'portugal': 'PT',
    'royaume-uni': 'GB',
    'suisse': 'CH',
    'suède': 'SE',
    'tchéquie': 'CZ',
    'république tchèque': 'CZ',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([a-zéû]+)\s+(20\d{2})\b', value.lower())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)\b', value.lower())
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def calendar_location(soup):
    link = soup.select_one('a.em-a2c-google[href*="location="]')
    if link is None:
        return ''
    return parse_qs(urlparse(link.get('href', '')).query).get('location', [''])[0].strip()


def parse_location(soup):
    where = soup.select_one('.em-event-where .em-event-location')
    venue_link = where.select_one('a[href*="/locations/"]') if where else None
    venue = clean_text(venue_link)
    location = calendar_location(soup)
    if not venue or not location:
        return None

    parts = [part.strip() for part in location.split(',') if part.strip()]
    country_code = COUNTRY_CODES.get(parts[-1].lower()) if parts else None
    if not country_code:
        return None

    # Events Manager emits addresses as street, city, postal code; its calendar
    # link appends the country. The city is consequently the penultimate address
    # component before the postal code, or the first component without a street.
    city = ''
    if len(parts) >= 3:
        city = parts[-3] if len(parts) >= 4 else parts[-2]
    if country_code in {'US', 'CA'} and len(parts) >= 5:
        city = parts[-4]

    city = re.sub(r'^\d{4,6}\s+', '', city).strip()
    if not city or re.fullmatch(r'[\d\s-]+', city):
        return None
    return venue, city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('.em-event-single')
    title = clean_text(soup.select_one('.entry-title'))
    event_date = parse_date(clean_text(event.select_one('.em-event-date')) if event else '')
    location = parse_location(soup)
    if not event or not title or not event_date or not location:
        return None

    venue, city, country_code = location
    description = clean_text(event.select_one('.em-event-content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(event.select_one('.em-event-time'))),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def get_with_retry(session, url, attempts=4):
    for attempt in range(attempts):
        response = session.get(url, timeout=45)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt + 1 < attempts:
            time.sleep(min(4 * (attempt + 1), 12))
    response.raise_for_status()


class LeConcertDAstreeFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='leconcertdastree_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            sitemap_response = get_with_retry(session, EVENT_SITEMAP_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Le Concert d’Astrée event sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=EVENT_SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(sitemap_response.content, 'xml')
        urls = [loc.get_text(strip=True) for loc in sitemap.select('url > loc')]
        records = []
        for url in urls:
            try:
                response = get_with_retry(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Le Concert d’Astrée event',
                    event='crawler_event_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_event(response.text, url)
            if record:
                records.append(record)
            time.sleep(0.25)

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    LeConcertDAstreeFrCrawler().run()


if __name__ == '__main__':
    main()
