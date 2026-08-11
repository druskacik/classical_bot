import re
import unicodedata
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://amarillis.fr/'
SOURCE = 'Ensemble Amarillis'
PAGE_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
PAGE_SLUGS = ('agenda', 'concerts-passes')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}

# The agenda is a touring calendar. These are places explicitly used by the
# source, not defaults inherited from the ensemble's home in Angers.
PLACES = {
    'aix-en-provence': ('Aix-en-Provence', 'FR'),
    'albertville': ('Albertville', 'FR'),
    'ambronay': ('Ambronay', 'FR'),
    'angers': ('Angers', 'FR'),
    'avignon': ('Avignon', 'FR'),
    'boulogne-billancourt': ('Boulogne-Billancourt', 'FR'),
    'brulon': ('Brûlon', 'FR'),
    'chambord': ('Chambord', 'FR'),
    'dijon': ('Dijon', 'FR'),
    'doue-en-anjou': ('Doué-en-Anjou', 'FR'),
    'edinbourg': ('Édimbourg', 'GB'),
    'entraigues-sur-la-sorgue': ('Entraigues-sur-la-Sorgue', 'FR'),
    'foussais-peyre': ('Foussais-Payré', 'FR'),
    'fontevraud': ('Fontevraud-l’Abbaye', 'FR'),
    'geneve': ('Genève', 'CH'),
    'madrid': ('Madrid', 'ES'),
    'magdeburg': ('Magdebourg', 'DE'),
    'maintenon': ('Maintenon', 'FR'),
    'marc-en-baroeul': ('Marcq-en-Barœul', 'FR'),
    'marcq-en-baroeul': ('Marcq-en-Barœul', 'FR'),
    'marseille': ('Marseille', 'FR'),
    'marson': ('Rou-Marson', 'FR'),
    'metz': ('Metz', 'FR'),
    'mirepoix': ('Mirepoix', 'FR'),
    'moisdon-la-riviere': ('Moisdon-la-Rivière', 'FR'),
    'monteneuf': ('Monteneuf', 'FR'),
    'mortiercrolles': ('Saint-Quentin-les-Anges', 'FR'),
    'nantes': ('Nantes', 'FR'),
    'paris': ('Paris', 'FR'),
    'romainmotier': ('Romainmôtier', 'CH'),
    'saint-michel-en-thierache': ('Saint-Michel-en-Thiérache', 'FR'),
    'saint nazaire': ('Saint-Nazaire', 'FR'),
    'saint-nazaire': ('Saint-Nazaire', 'FR'),
    'saintes': ('Saintes', 'FR'),
    'sable-sur-sarthe': ('Sablé-sur-Sarthe', 'FR'),
    'saumur': ('Saumur', 'FR'),
    'savennières': ('Savennières', 'FR'),
    'savennieres': ('Savennières', 'FR'),
    'seoul': ('Séoul', 'KR'),
    'toulouse': ('Toulouse', 'FR'),
    'tourcoing': ('Tourcoing', 'FR'),
    'treves': ('Trèves', 'FR'),
    'valloire': ('Valloire', 'FR'),
    'versailles': ('Versailles', 'FR'),
    'vivoin': ('Vivoin', 'FR'),
    'zug': ('Zoug', 'CH'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip(' |')


def folded(value):
    value = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in value if not unicodedata.combining(char))


def parse_dates(value):
    text = folded(value)
    split_numeric = re.search(
        r'\b(\d{1,2})\s+(?:et|&)\s+(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(20\d{2})\b',
        text,
    )
    if split_numeric:
        try:
            return [
                date(int(split_numeric.group(4)), int(split_numeric.group(3)), day)
                for day in (int(split_numeric.group(1)), int(split_numeric.group(2)))
            ]
        except ValueError:
            return []
    numeric = re.search(r'\b(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(20\d{2})\b', text)
    if numeric:
        try:
            return [date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1)))]
        except ValueError:
            return []

    match = re.search(
        r'\b(?:du\s+)?(\d{1,2})(?:\s+(?:au|et|&|-)\s+(\d{1,2}))?\s+'
        r'([a-z]+)\s+(20\d{2})\b', text,
    )
    if not match or match.group(3) not in MONTHS:
        return []
    try:
        start = date(int(match.group(4)), MONTHS[match.group(3)], int(match.group(1)))
        end = date(start.year, start.month, int(match.group(2) or match.group(1)))
    except ValueError:
        return []
    # Long ranges on this site describe academies/residencies rather than a
    # concert on every day. Keep only explicitly bounded short runs.
    if (end - start).days > 3:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', folded(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def find_place(value):
    text = folded(value)
    matches = []
    for key, place in PLACES.items():
        position = text.find(folded(key))
        if position >= 0:
            matches.append((position, -len(key), place))
    return min(matches)[2] if matches else (None, None)


def is_date_heading(value):
    return bool(parse_dates(value))


def parse_block(block, page_url):
    headings = [clean_text(node) for node in block.find_all(['h2', 'h3', 'h4', 'h5'])]
    all_text = clean_text(block)
    dates = []
    for heading in headings:
        dates = parse_dates(heading)
        if dates:
            break
    if not dates:
        return []

    location_index = None
    city = country_code = None
    for index, heading in enumerate(headings):
        candidate_city, candidate_country = find_place(heading)
        if candidate_city:
            location_index, city, country_code = index, candidate_city, candidate_country
            break
    if not city:
        city, country_code = find_place(all_text[:500])
    if not city:
        return []

    candidates = [
        (index, heading) for index, heading in enumerate(headings)
        if not is_date_heading(heading) and index != location_index and not parse_time(heading)
    ]
    # Programme titles normally follow the location; fall back to the last
    # non-date heading for older blocks whose order differs.
    after_location = [item for item in candidates if location_index is not None and item[0] > location_index]
    title = clean_text((after_location or candidates)[-1][1]) if candidates else ''
    if not title:
        return []

    location_text = headings[location_index] if location_index is not None else ''
    venue = re.sub(r'\b(?:[01]?\d|2[0-3])\s*h\s*[0-5]?\d*\b', '', location_text, flags=re.I)
    venue = clean_text(venue.replace('|', ' '))
    venue = re.sub(r'\s+à partir de\s*$', '', venue, flags=re.I)
    if not venue or folded(venue) == folded(city):
        return []

    time_from = parse_time(location_text) or parse_time(all_text[:350])
    description_parts = []
    for node in block.find_all(['p', 'ul']):
        text = clean_text(node)
        if text and not re.fullmatch(r'(billetterie|informations?|reservations?).*', folded(text)):
            if text not in description_parts:
                description_parts.append(text)
    description = '\n\n'.join(description_parts) or None
    return [{
        'title': title,
        'date': event_date.isoformat(),
        'url': page_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    } for event_date in dates]


def fetch_page(session, slug):
    response = session.get(PAGE_API, params={'slug': slug, 'context': 'view'}, timeout=45)
    response.raise_for_status()
    pages = response.json()
    if not pages:
        return None
    return pages[0]


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    records = []
    for slug in PAGE_SLUGS:
        try:
            page = fetch_page(session, slug)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Amarillis calendar page',
                event='crawler_page_failed', level='warning',
                url=f'{SOURCE_URL}{slug}/', error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if not page:
            continue
        soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
        for block in soup.select('.wp-block-columns'):
            records.extend(parse_block(block, page['link']))
    return sorted(records, key=lambda row: (
        row['date'], row['time_from'] or '', row['title'], row['venue']
    ))


class AmarillisFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amarillis_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
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
    AmarillisFrCrawler().run()


if __name__ == '__main__':
    main()
