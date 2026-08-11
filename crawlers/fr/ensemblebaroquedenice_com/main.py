import html
import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ensemblebaroquedenice.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concert'
SOURCE = 'Ensemble Baroque de Nice'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}

# These corrections are supported by the programme text on the corresponding
# detail pages. They resolve abbreviated or erroneous location summaries.
LOCATION_OVERRIDES = {
    'le-mois-de-juillet-de-lensemble-baroque-de-nice': (
        'Église Notre-Dame-de-l’Assomption', 'Callas'
    ),
    'les-premiers-maitres-du-violon': ('Monastère de Saorge', 'Saorge'),
    'tempetes-baroques': ('Cathédrale', 'Nice'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFD', value.lower())
        if unicodedata.category(character) != 'Mn'
    )


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\s+[àa]\s+(\d{1,2}):(\d{2})',
        clean_text(value),
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    return event_date, f'{int(match.group(4)):02d}:{match.group(5)}'


def parse_location(value, slug):
    if slug in LOCATION_OVERRIDES:
        return LOCATION_OVERRIDES[slug]

    location = re.sub(r'\s*/\s*(?:France)?\s*$', '', clean_text(value)).strip()
    location = re.sub(r'\s*\([^)]*\)\s*$', '', location).strip()
    location = re.sub(r'\b\d{5}\s+', '', location)
    if ',' in location:
        venue, city = (part.strip(' –-') for part in location.rsplit(',', 1))
        if venue and city:
            return venue, city
    # The recurring Saint-Martin venue is sometimes separated from Nice with
    # punctuation rather than a comma.
    if normalized(location).endswith(' nice'):
        venue = re.sub(r'[ ,–-]+Nice$', '', location, flags=re.IGNORECASE).strip()
        if venue:
            return venue, 'Nice'
    return None, None


def section_text(soup, heading):
    for element in soup.select('h2'):
        if normalized(clean_text(element)) == normalized(heading):
            return clean_text(element.parent)
    return ''


def parse_detail(page_html, url, slug):
    soup = BeautifulSoup(page_html, 'html.parser')
    headings = soup.select('h2')
    if not headings:
        return None

    event_date, time_from = parse_datetime(headings[0])
    location_element = headings[0].find_next_sibling('p')
    venue, city = parse_location(location_element, slug)

    title = clean_text(headings[2]) if len(headings) > 2 else ''
    description_parts = []
    for heading in ('Edito', 'Programme', 'Ensemble Baroque de Nice', 'Autour des concerts'):
        text = section_text(soup, heading)
        if text and text != heading:
            description_parts.append(text)

    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url.rstrip('/'),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EnsembleBaroqueDeNiceComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemblebaroquedenice_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            response = session.get(
                API_URL, params={'per_page': 100, 'page': page}, timeout=45
            )
            response.raise_for_status()
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            for item in response.json():
                url = item['link']
                try:
                    detail = session.get(url, timeout=45)
                    detail.raise_for_status()
                    record = parse_detail(detail.text, url, item['slug'])
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Ensemble Baroque de Nice concert',
                            event='crawler_item_skipped', level='warning', url=url,
                            error_type='IncompleteEventData',
                            error_message='Required date, city, or venue could not be resolved',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Ensemble Baroque de Nice concert',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
            page += 1

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    EnsembleBaroqueDeNiceComCrawler().run()


if __name__ == '__main__':
    main()
