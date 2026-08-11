import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lesombres.fr/'
SOURCE = 'Les Ombres'
EVENT_SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

COUNTRY_NAMES = {
    'allemagne': 'DE', 'belgique': 'BE', 'espagne': 'ES',
    'france': 'FR', 'italie': 'IT', 'japon': 'JP', 'lettonie': 'LV',
    'pays-bas': 'NL', 'royaume-uni': 'GB', 'suisse': 'CH',
    'germany': 'DE', 'belgium': 'BE', 'spain': 'ES', 'italy': 'IT',
    'japan': 'JP', 'latvia': 'LV', 'netherlands': 'NL',
    'united kingdom': 'GB', 'switzerland': 'CH',
}

# Foreign tour listings normally omit the country but consistently name the city.
FOREIGN_CITIES = {
    'bâle': 'CH', 'basel': 'CH', 'bruxelles': 'BE', 'brussels': 'BE',
    'cēsis': 'LV', 'cesis': 'LV', 'riga': 'LV', 'tokyo': 'JP',
    'utrecht': 'NL', 'york': 'GB',
}


def clean_text(node):
    if node is None:
        return ''
    text = node.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'\b([a-zA-ZÀ-ÿ]+)\s+(\d{1,2}),\s*(20\d{2})'
        r'(?:\s+([01]?\d|2[0-3]):([0-5]\d))?',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(1).casefold())
    if not month:
        return None, None
    try:
        event_date = datetime(
            int(match.group(3)), month, int(match.group(2))
        ).date().isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{match.group(5)}'
    return event_date, time_from


def parse_location(value):
    value = re.sub(r'^lieu\s*:\s*', '', value, flags=re.IGNORECASE).strip()
    country_match = re.search(r'\(([^()]*)\)\s*$', value)
    country_hint = country_match.group(1).strip() if country_match else ''
    # A numeric parenthesis is a French department marker, not a country.
    if country_match and not country_hint.isdigit():
        value = value[:country_match.start()].strip()

    separator = ',' if ',' in value else ' - '
    parts = [part.strip(' -') for part in value.rsplit(separator, 1) if part.strip(' -')]
    if len(parts) < 2:
        return None
    venue = ', '.join(parts[:-1]).strip()
    city = re.sub(r'\s*\(\d{2,3}\)\s*$', '', parts[-1]).strip()
    if not venue or not city or re.search(r'\b\d{4,6}\b', city):
        return None

    folded = f'{value} {country_hint}'.casefold()
    country_code = 'FR'
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'\b{re.escape(name)}\b', folded):
            country_code = code
            break
    else:
        country_code = FOREIGN_CITIES.get(city.casefold(), 'FR')
    return venue, city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('#top_event_single_title2'))
    event_date, time_from = parse_datetime(
        clean_text(soup.select_one('.qodef-event-details-time'))
    )
    location = parse_location(
        clean_text(soup.select_one('.qodef-event-details-location'))
    )
    if not title or not event_date or not location:
        return None

    venue, city, country_code = location
    description = clean_text(soup.select_one('.qodef-event-single-content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class LesOmbresFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lesombres_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        response = requests.get(EVENT_SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.content, 'xml')
        urls = [
            loc.get_text(strip=True)
            for loc in sitemap.select('url > loc')
            if loc.get_text(strip=True).rstrip('/')
            != f'{SOURCE_URL}agenda-details'.rstrip('/')
        ]

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Les Ombres event',
                        event='crawler_event_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(records, key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['url']
        ))


def main():
    LesOmbresFrCrawler().run()


if __name__ == '__main__':
    main()
