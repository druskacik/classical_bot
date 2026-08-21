import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.juliusdrake.com/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Julius Drake'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRIES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'canada': 'CA',
    'china': 'CN', 'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK',
    'finland': 'FI', 'france': 'FR', 'germany': 'DE', 'hungary': 'HU',
    'ireland': 'IE', 'italy': 'IT', 'japan': 'JP', 'luxembourg': 'LU',
    'netherlands': 'NL', 'norway': 'NO', 'poland': 'PL', 'portugal': 'PT',
    'romania': 'RO', 'singapore': 'SG', 'slovakia': 'SK', 'slovenia': 'SI',
    'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'united kingdom': 'GB', 'uk': 'GB', 'england': 'GB', 'scotland': 'GB',
    'wales': 'GB', 'united states': 'US', 'usa': 'US', 'u.s.a.': 'US',
}

# Venue calendars often omit the country from individual occurrences. These
# are stable, internationally recognisable locations present in the archive.
PLACE_COUNTRIES = {
    'amsterdam': 'NL', 'barcelona': 'ES', 'berlin': 'DE', 'birmingham': 'GB',
    'brno': 'CZ', 'brussels': 'BE', 'cambridge': 'GB', 'cologne': 'DE',
    'dresden': 'DE', 'edinburgh': 'GB', 'eisenstadt': 'AT', 'florence': 'IT',
    'frankfurt': 'DE', 'geneva': 'CH', 'glasgow': 'GB', 'graz': 'AT',
    'hamburg': 'DE', 'helsinki': 'FI', 'innsbruck': 'AT', 'lausanne': 'CH',
    'leeds': 'GB', 'leipzig': 'DE', 'liège': 'BE', 'london': 'GB',
    'luxembourg': 'LU', 'madrid': 'ES', 'manchester': 'GB', 'melbourne': 'AU',
    'munich': 'DE', 'münchen': 'DE', 'new york': 'US', 'oslo': 'NO',
    'oxford': 'GB', 'paris': 'FR', 'prague': 'CZ', 'salzburg': 'AT',
    'san francisco': 'US', 'siena': 'IT', 'singapore': 'SG', 'stockholm': 'SE',
    'sydney': 'AU', 'toblach': 'IT', 'toronto': 'CA', 'venice': 'IT',
    'vienna': 'AT', 'washington': 'US', 'zurich': 'CH',
    'zürich': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    return list(dict.fromkeys(
        clean_text(node) for node in soup.select('url > loc')
        if '/events/' in clean_text(node)
    ))


def country_for(text):
    lowered = text.casefold()
    for name, code in sorted(COUNTRIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', lowered):
            return code
    for place, code in sorted(PLACE_COUNTRIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf'(?<!\w){re.escape(place)}(?!\w)', lowered):
            return code
    return None


def city_for(title, address, venue):
    # Prefer the advertised event title over venue metadata: old copied events
    # occasionally retain the previous event's location fields.
    for evidence in (title, address, venue):
        lowered = evidence.casefold()
        matches = [place for place in PLACE_COUNTRIES if re.search(
            rf'(?<!\w){re.escape(place)}(?!\w)', lowered
        )]
        if matches:
            return max(matches, key=len).title()

    # For a conventional comma-separated postal address, the locality is the
    # component immediately before the country or state/postcode component.
    parts = [part.strip() for part in address.split(',') if part.strip()]
    if len(parts) >= 2:
        candidates = parts[1:-1] or parts[1:]
        for candidate in reversed(candidates):
            candidate = re.sub(r'\b[A-Z]{1,3}\d[A-Z\d -]*\b.*$', '', candidate).strip()
            candidate = re.sub(r'^\d+\s+', '', candidate).strip()
            if candidate and not country_for(candidate) and not re.search(r'\d', candidate):
                return candidate
    return None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    event = soup.select_one('.eventon_list_event')
    if not event:
        return None

    title = clean_text(event.select_one('.evcal_event_title, [itemprop="name"]'))
    date_node = event.select_one('meta[itemprop="startDate"]')
    raw_date = date_node.get('content', '') if date_node else ''
    try:
        event_date = datetime.strptime(raw_date, '%Y-%m-%d').date().isoformat()
    except ValueError:
        try:
            event_date = datetime.strptime(raw_date, '%Y-%m-%dT%H:%M:%S%z').date().isoformat()
        except ValueError:
            return None

    venue = clean_text(event.select_one('.evo_location_name, .event_location_name'))
    address = clean_text(event.select_one('.evo_location_address'))
    description = clean_text(event.select_one('.eventon_desc_in')) or None
    schema_url = event.select_one('.evo_event_schema [itemprop="url"]')
    canonical_url = schema_url.get('href', '').strip() if schema_url else url
    canonical_url = canonical_url if '/events/' in canonical_url else url
    time_text = clean_text(event.select_one('.evcal_time, .evo_start .time'))
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', time_text)
    time_from = time_match.group(0) if time_match else None

    title_country = country_for(title)
    metadata_country = country_for(address) or country_for(venue)
    country_code = title_country or metadata_country
    city = city_for(title, address, venue)
    if title_country and metadata_country and title_country != metadata_country:
        advertised_venue = re.split(r'\s+with\s+', title, maxsplit=1, flags=re.IGNORECASE)[0]
        if city and city.casefold() in advertised_venue.casefold():
            venue = advertised_venue.strip(' ,-')
    if not all((title, venue, city, country_code, canonical_url)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': canonical_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Julius Drake event detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['url']
    ))


class JuliusDrakeComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='juliusdrake_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    JuliusDrakeComCrawler().run()


if __name__ == '__main__':
    main()
