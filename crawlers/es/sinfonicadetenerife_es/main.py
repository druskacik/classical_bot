import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sinfonicadetenerife.es/'
SOURCE = 'Orquesta Sinfónica de Tenerife'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concierto'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

# These are Elementor template element IDs, shared by the retained concert
# archive as well as the current season.
DATE_SELECTOR = '.elementor-element-0ebe6ed .elementor-widget-container'
VENUE_SELECTOR = '.elementor-element-b307d90 .elementor-widget-container'
ROOM_SELECTOR = '.elementor-element-5de52a9a .elementor-widget-container'
TIME_SELECTOR = '.elementor-element-f7c456f .elementor-widget-container'

DATE_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b')
TIME_RE = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\b')

MISSING_VENUES = {
    'a12-la-quinta-de-prokofiev': 'Auditorio de Tenerife Adán Martín',
    'a13-la-grande': 'Auditorio de Tenerife Adán Martín',
    'a16-noches-en-los-jardines-de-espana': 'Auditorio de Tenerife Adán Martín',
    'a17-las-siete-ultimas-palabras-de-cristo': 'Auditorio de Tenerife Adán Martín',
    'a18-un-mundo-nuevo': 'Auditorio de Tenerife Adán Martín',
    'festival-internacional-de-santander': 'Palacio de Festivales de Cantabria',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def city_for_venue(venue):
    key = venue.casefold()
    places = (
        ('la orotava', 'La Orotava'),
        ('puerto de la cruz', 'Puerto de la Cruz'),
        ('san cristóbal de la laguna', 'San Cristóbal de La Laguna'),
        ('la laguna', 'San Cristóbal de La Laguna'),
        ('santa cruz de tenerife', 'Santa Cruz de Tenerife'),
        ('santa cruz', 'Santa Cruz de Tenerife'),
        ('los cristianos', 'Arona'),
        ('arona', 'Arona'),
        ('guía de isora', 'Guía de Isora'),
        ('candelaria', 'Candelaria'),
        ('garachico', 'Garachico'),
        ('los realejos', 'Los Realejos'),
        ('el sauzal', 'El Sauzal'),
        ('granadilla', 'Granadilla de Abona'),
        ('adeje', 'Adeje'),
        ('s/c de tenerife', 'Santa Cruz de Tenerife'),
        ('s/c de la palma', 'Santa Cruz de La Palma'),
        ('de la palma', 'Santa Cruz de La Palma'),
        ('los silos', 'Los Silos'),
        ('fuerteventura', 'Puerto del Rosario'),
        ('jameos del agua', 'Haría'),
        ('alfredo kraus', 'Las Palmas de Gran Canaria'),
        ('euskalduna', 'Bilbao'),
        ('pollença', 'Pollença'),
        ('la gomera', 'San Sebastián de La Gomera'),
        ('auditorio de la peña', 'Valverde'),
        ('concertgebouw', 'Amsterdam'),
        ('infanta leonor', 'Arona'),
        ('hospital universitario de canarias', 'San Cristóbal de La Laguna'),
        ('palacio de festivales de cantabria', 'Santander'),
    )
    for marker, city in places:
        if marker in key:
            return city

    # The orchestra's main venue is in Santa Cruz. Its pages use both the
    # current short name and the former "Adán Martín" name.
    if 'auditorio de tenerife' in key:
        return 'Santa Cruz de Tenerife'
    return None


def programme_text(soup):
    heading = next(
        (node for node in soup.select('h2, h3') if clean_text(node).casefold() == 'programa'),
        None,
    )
    if not heading:
        return ''

    section = heading.find_parent(class_='e-con-boxed')
    if not section:
        section = heading.parent
    lines = []
    started = False
    for value in section.stripped_strings:
        value = clean_text(value)
        if value.casefold() == 'programa':
            started = True
            continue
        if started and value.casefold() in {
            'pre-concierto', 'abonos', 'lanzadera', 'sobre las entradas',
        }:
            break
        if started and value and value not in lines:
            lines.append(value)
    return '\n'.join(lines)


def parse_detail(post, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    title = clean_text((post.get('title') or {}).get('rendered'))
    url = post.get('link') or ''
    event_date = parse_date(clean_text(soup.select_one(DATE_SELECTOR)))
    venue = clean_text(soup.select_one(VENUE_SELECTOR)).strip(' ,;–-')
    if not venue:
        venue = MISSING_VENUES.get(post.get('slug') or '') or MISSING_VENUES.get(
            url.rstrip('/').rsplit('/', 1)[-1]
        ) or ''
    room = clean_text(soup.select_one(ROOM_SELECTOR)).strip(' ,;–-')
    if room and room.casefold() not in venue.casefold():
        venue = f'{venue} – {room}' if venue else room
    city = city_for_venue(venue)

    time_match = TIME_RE.search(clean_text(soup.select_one(TIME_SELECTOR)))
    time_from = (
        f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        if time_match else None
    )

    body = clean_text((post.get('content') or {}).get('rendered'))
    programme = programme_text(soup)
    description = '\n\n'.join(
        part for part in (body, f'Programa\n{programme}' if programme else '') if part
    ) or None

    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'NL' if city == 'Amsterdam' else 'ES',
        'description': description,
    }


def fetch_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'id,link,title,content',
            },
            timeout=60,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return posts


def fetch_record(post):
    response = requests.get(post['link'], headers=HEADERS, timeout=60)
    response.raise_for_status()
    return parse_detail(post, response.text)


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = fetch_posts(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_record, post): post for post in posts}
        for future in as_completed(futures):
            post = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped concert with incomplete date or location',
                        event='crawler_item_skipped',
                        level='warning',
                        url=post.get('link'),
                    )
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to fetch or parse Sinfónica de Tenerife concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=post.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class SinfonicaDeTenerifeEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonicadetenerife_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SinfonicaDeTenerifeEsCrawler().run()


if __name__ == '__main__':
    main()
