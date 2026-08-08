import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filarmonica.art.br/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'Orquestra Filarmônica de Minas Gerais'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}

# These are venue terms used by the site's own event taxonomy. Locations in
# Belo Horizonte omit the city because it is the orchestra's home calendar.
HOME_VENUES = {
    'sala minas gerais',
    'biblioteca pública estadual',
    'parque ecológico da pampulha',
    'praça da assembleia',
    'praça da liberdade',
    'teatro da biblioteca pública estadual',
    'teatro josé aparecido de oliveira - biblioteca pública estadual',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response


def event_catalogue():
    events = []
    page = 1
    while True:
        response = get_response(
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'id,link,title,slug',
            },
        )
        events.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            return events
        page += 1


def resolve_city(venue):
    normalized = clean_text(venue).lower()
    if normalized in HOME_VENUES:
        return 'Belo Horizonte'
    if 'hospital da baleia' in normalized or normalized.endswith('| bh'):
        return 'Belo Horizonte'
    if 'burle marx' in normalized:
        return 'Belo Horizonte'
    if normalized == 'praça orides parreiras':
        return 'Brumadinho'

    # Touring locations are consistently written after a pipe or hyphen.
    match = re.search(r'(?:\||\s-\s)([^|]+)$', venue)
    if match:
        city = clean_text(match.group(1))
        city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city).strip()
        if city.upper() == 'BH':
            return 'Belo Horizonte'
        return city
    return None


def parse_performance(value, year):
    text = clean_text(value).lower()
    match = re.search(
        r'(\d{1,2})\s+([a-zç]{3})\.?[^-–]*[-–]\s*(\d{1,2})h(?:(\d{2}))?',
        text,
    )
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        event_date = date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None
    return event_date, f'{int(match.group(3)):02d}:{match.group(4) or "00"}'


def event_description(container):
    parts = []
    works_heading = next(
        (
            heading for heading in container.find_all(['h4', 'h5'])
            if clean_text(heading.get_text()).upper() == 'OBRAS'
        ),
        None,
    )
    if works_heading:
        works = clean_text(works_heading.parent.get_text('\n', strip=True))
        if works and works.upper() != 'OBRAS':
            parts.append(works)

    # The editorial programme body is presented as a Fraunces h4. It is kept
    # in addition to the structured works so composer/work extraction has the
    # richest text the page publishes.
    for paragraph in container.select('h4.fraunces.lh-copy'):
        text = clean_text(paragraph.get_text('\n', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(event):
    url = event.get('link') or ''
    if not url:
        return []
    soup = BeautifulSoup(get_response(url).text, 'html.parser')
    container = soup.select_one('.single-concerto')
    if not container:
        return []

    title_node = container.select_one('h3.ttu.fraunces')
    title = clean_text(title_node.get_text()) if title_node else clean_text(
        (event.get('title') or {}).get('rendered')
    )
    year_match = re.search(r'\b(20\d{2})\b', f'{title} {event.get("slug", "")}')
    venue_node = container.select_one('.info-column h6')
    venue = clean_text(venue_node.get_text()) if venue_node else ''
    city = resolve_city(venue)
    if not title or not year_match or not venue or not city:
        return []

    description = event_description(container)
    records = []
    for date_node in container.select('h5.degular.fw3.ttu.mv1'):
        performance = parse_performance(date_node.get_text(' ', strip=True), int(year_match.group(1)))
        if not performance:
            continue
        event_date, event_time = performance
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


def get_concerts():
    events = event_catalogue()
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FilarmonicaArtBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonica_art_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FilarmonicaArtBrCrawler().run()


if __name__ == '__main__':
    main()
