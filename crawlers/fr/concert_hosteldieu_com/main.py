import re
import unicodedata
from datetime import date, timedelta
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concert-hosteldieu.com/'
SOURCE = "Le Concert de l'Hostel Dieu"
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
PAGES_API = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'janv': 1, 'fevrier': 2, 'fevr': 2, 'mars': 3,
    'avril': 4, 'avr': 4, 'mai': 5, 'juin': 6, 'juillet': 7,
    'juil': 7, 'aout': 8, 'septembre': 9, 'sept': 9, 'octobre': 10,
    'oct': 10, 'novembre': 11, 'nov': 11, 'decembre': 12, 'dec': 12,
}

COUNTRY_MARKERS = {
    'armenie': 'AM', 'belgique': 'BE', 'colombie': 'CO', 'italie': 'IT',
    'allemagne': 'DE', 'espagne': 'ES', 'pays-bas': 'NL', 'suisse': 'CH',
    'royaume-uni': 'GB', 'autriche': 'AT', 'luxembourg': 'LU',
}

CITY_COUNTRIES = {
    'bogota': 'CO', 'erevan': 'AM', 'namur': 'BE', 'bruges': 'BE',
    'viterbo': 'IT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', clean_text(value))
    return ''.join(character for character in value if not unicodedata.combining(character)).lower()


def parse_date(value, default_year=None):
    text = normalized(value).replace('.', ' ')
    match = re.search(r'\b(\d{1,2})\s+([a-z]+)(?:\s+(20\d{2}))?\b', text)
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    year = int(match.group(3) or default_year or 0)
    if not month or not year:
        return None
    try:
        return date(year, month, int(match.group(1)))
    except ValueError:
        return None


def parse_date_range(value, default_year=None):
    text = normalized(value)
    range_match = re.search(r'\bdu\s+(\d{1,2})\s+au\s+(\d{1,2})\s+([a-z.]+)(?:\s+(20\d{2}))?', text)
    if not range_match:
        parsed = parse_date(value, default_year)
        return [parsed] if parsed else []
    end = parse_date(
        f'{range_match.group(2)} {range_match.group(3)} {range_match.group(4) or default_year}',
    )
    if not end:
        return []
    try:
        start = date(end.year, end.month, int(range_match.group(1)))
    except ValueError:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*(?:h|:)\s*([0-5]\d)?\b', normalized(value))
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def country_for(city):
    text = normalized(city)
    for marker, code in COUNTRY_MARKERS.items():
        if marker in text:
            return code
    plain_city = re.split(r'\s*[,(-]\s*', text)[0]
    return CITY_COUNTRIES.get(plain_city, 'FR')


def clean_city(city):
    value = re.sub(r'\s*\((?:Arménie|Belgique|Colombie|Italie)\)\s*$', '', clean_text(city), flags=re.I)
    value = re.sub(r'\s*,\s*(?:Belgique|Colombie|Italie)\s*$', '', value, flags=re.I)
    return value.strip()


def choose_url(card):
    info = card.select_one('a.agendaInfo[href]')
    ticket = card.select_one('a.agendaBillet[href]')
    info_url = urljoin(SOURCE_URL, info.get('href')) if info else ''
    if info_url.rstrip('/') == SOURCE_URL.rstrip('/'):
        return ticket.get('href') if ticket else AGENDA_URL
    return info_url or (ticket.get('href') if ticket else AGENDA_URL)


def parse_agenda_card(card):
    title = clean_text(card.select_one('.agendaTitle'))
    if normalized(title).startswith(('conference', 'rencontre')):
        return []
    event_date = parse_date(card.select_one('.agendaJour'))
    location_values = [clean_text(item) for item in card.select('.agendaLieu > div') if clean_text(item)]
    if len(location_values) < 2:
        return []
    venue, city = location_values[0], location_values[-1]

    # A few tour entries put "venue - city" in the city field and a festival
    # name in the venue field. Preserve the concrete venue and split the city.
    if ' - ' in city:
        possible_venue, possible_city = [part.strip() for part in city.rsplit(' - ', 1)]
        if possible_venue and possible_city:
            venue, city = possible_venue, possible_city
    elif normalized(city).startswith('temple du mazet-saint-voy'):
        venue, city = city, 'Le Mazet-Saint-Voy'

    city = clean_city(city)
    if not title or not event_date or not venue or not city:
        return []
    return [{
        'title': title,
        'date': event_date.isoformat(),
        'url': choose_url(card),
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_for(city),
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


def season_year(element):
    previous = element.find_previous(['h3', 'h4'])
    if not previous:
        return None
    match = re.search(r'(20\d{2})', clean_text(previous))
    return int(match.group(1)) if match else None


def parse_season_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for heading in soup.find_all(['h3', 'h4']):
        title = clean_text(heading)
        previous_heading = heading.find_previous(['h3', 'h4'])
        if not title or not previous_heading or not re.search(
            r'20\d{2}', clean_text(previous_heading)
        ):
            continue
        default_year = season_year(heading)
        siblings = []
        for item in heading.next_siblings:
            item_name = getattr(item, 'name', None)
            item_text = clean_text(item)
            if item_name == 'h3':
                break
            if item_name == 'h4' and not parse_date_range(item_text, default_year):
                break
            siblings.append(item)
        text_elements = [clean_text(item) for item in siblings if clean_text(item)]
        date_index = next(
            (index for index, text in enumerate(text_elements) if parse_date_range(text, default_year)),
            None,
        )
        if date_index is None:
            continue
        date_line = text_elements[date_index]
        dates = parse_date_range(date_line, default_year)
        venue_parts = re.split(r'\n|\s*\|\s*', date_line)
        venue_parts = [part.strip() for part in venue_parts if part.strip()]
        venue = ''
        for part in venue_parts[1:]:
            without_time = re.sub(r'^\s*(?:[01]?\d|2[0-3])\s*(?:h|:)\s*(?:[0-5]\d)?\s*', '', part, flags=re.I)
            if without_time:
                venue = without_time
        if not venue and date_index + 1 < len(text_elements):
            possible_venue = text_elements[date_index + 1]
            if not re.search(r'\b(?:information|billetterie)\b', normalized(possible_venue)):
                venue = possible_venue
        if not dates or not venue:
            continue
        time_from = parse_time(date_line)
        description_parts = []
        info_url = ''
        for item in siblings:
            if getattr(item, 'name', None) in ('h3', 'h4'):
                break
            if getattr(item, 'name', None) == 'p':
                text = clean_text(item)
                if text and not re.search(r'\b(?:information|billetterie)\b', normalized(text)):
                    description_parts.append(text)
                for link in item.select('a[href]'):
                    if 'information' in normalized(link):
                        info_url = urljoin(SOURCE_URL, link.get('href'))
        event_url = info_url or page_url
        for event_date in dates:
            records.append({
                'title': title,
                'date': event_date.isoformat(),
                'url': event_url,
                'time_from': time_from,
                'venue': venue,
                'city': 'Lyon',
                'country_code': 'FR',
                'description': '\n\n'.join(description_parts) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def fetch_description(session, url):
    parts = urlsplit(url)
    if parts.netloc != urlsplit(SOURCE_URL).netloc:
        return None
    slug = parts.path.rstrip('/').split('/')[-1]
    if not slug or slug in {'agenda'} or slug.startswith('saison-'):
        return None
    response = session.get(PAGES_API, params={'slug': slug, '_fields': 'content'}, timeout=45)
    response.raise_for_status()
    pages = response.json()
    if not pages:
        return None
    soup = BeautifulSoup(pages[0]['content']['rendered'], 'html.parser')
    for element in soup.select('script, style, form, nav'):
        element.decompose()
    return clean_text(soup) or None


class ConcertHosteldieuComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concert_hosteldieu_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        response = session.get(AGENDA_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for card in soup.select('article.agendaItem'):
            parsed = parse_agenda_card(card)
            if parsed:
                records.extend(parsed)
            else:
                log_message(
                    'Skipped non-concert or incomplete Hostel Dieu agenda item',
                    event='crawler_item_skipped',
                    level='warning',
                    url=choose_url(card),
                    error_type='OutOfScopeOrIncompleteEvent',
                    error_message='Item is not a concert or lacks a defensible date, city, or venue',
                )

        page_number = 1
        while True:
            pages_response = session.get(
                PAGES_API,
                params={'per_page': 100, 'page': page_number, '_fields': 'slug,link,content'},
                timeout=45,
            )
            if pages_response.status_code == 400 and page_number > 1:
                break
            pages_response.raise_for_status()
            pages = pages_response.json()
            for page in pages:
                if re.fullmatch(r'saison-20\d{2}-20\d{2}(?:-[a-z]+)?', page['slug']):
                    records.extend(parse_season_page(page['content']['rendered'], page['link']))
            if page_number >= int(pages_response.headers.get('X-WP-TotalPages', page_number)):
                break
            page_number += 1

        descriptions = {}
        for url in sorted({record['url'] for record in records}):
            try:
                descriptions[url] = fetch_description(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Hostel Dieu production description',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        for record in records:
            record['description'] = descriptions.get(record['url']) or record['description']

        merged = {}
        for record in records:
            key = (record['title'], record['date'], record['venue'], record['city'])
            existing = merged.get(key)
            if not existing:
                merged[key] = record
                continue
            if record['time_from'] and not existing['time_from']:
                existing['time_from'] = record['time_from']
            if record['description'] and not existing['description']:
                existing['description'] = record['description']
            if existing['url'] == AGENDA_URL and record['url'] != AGENDA_URL:
                existing['url'] = record['url']

        return sorted(
            merged.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    ConcertHosteldieuComCrawler().run()


if __name__ == '__main__':
    main()
