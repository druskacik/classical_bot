import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filarmed.org/'
SOURCE = 'Orquesta Filarmónica de Medellín'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
PROGRAMME_URL = f'{SOURCE_URL}programacion/'
SEASON_URL = f'{SOURCE_URL}temporada-2026-filarmed/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-CO,es;q=0.9',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}


def clean_text(element):
    if element is None:
        return ''
    value = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_spanish_date(value, year):
    match = re.search(
        r'\b(\d{1,2})\s+de\s+'
        r'(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return date(year, MONTHS[match.group(2).lower()], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([ap])\.?\s*m\.?', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def labelled_value(text, label, following_labels):
    end = '|'.join(re.escape(item) for item in following_labels)
    match = re.search(rf'\b{re.escape(label)}\s+(.+?)(?=\s+(?:{end})\b|$)', text, re.IGNORECASE)
    return match.group(1).strip() if match else ''


def base_record(title, event_date, url, time_from, venue, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Medellín',
        'country_code': 'CO',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_event_page(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    text = clean_text(soup)
    event_date = parse_spanish_date(labelled_value(text, 'Fecha', ['Horario']), 2026)
    venue = labelled_value(text, 'Ubicación', ['Valores de boletería', 'Adquirir'])
    if not venue:
        venue = labelled_value(text, 'Lugar', ['Artistas invitados', 'Tipo de público'])
    heading = soup.find(['h1', 'h2'], string=re.compile(r'Concierto', re.IGNORECASE))
    title = clean_text(heading) or clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser'))

    description_heading = soup.find(
        lambda tag: tag.name in ('h2', 'h3', 'h4')
        and '¿por qué asistir' in clean_text(tag).lower()
    )
    description_parts = []
    if description_heading:
        for node in description_heading.find_all_next(['p', 'h2', 'h3', 'h4']):
            node_text = clean_text(node)
            if re.search(r'^(adquirir|fecha\b)', node_text, re.IGNORECASE):
                break
            if node.name == 'p' and node_text:
                description_parts.append(node_text)
    description = ' '.join(dict.fromkeys(description_parts)) or None

    if not all((title, event_date, venue, page.get('link'))):
        return None
    return base_record(title, event_date, page['link'], parse_time(text), venue, description)


def season_title_map(soup):
    titles = {}
    for heading in soup.find_all(['h2', 'h3']):
        match = re.fullmatch(r'Concierto\s+(\d+)', clean_text(heading), re.IGNORECASE)
        if not match:
            continue
        next_heading = heading.find_next(['h2', 'h3'])
        candidate = clean_text(next_heading)
        if candidate and not re.fullmatch(r'Concierto\s+\d+', candidate, re.IGNORECASE):
            titles[int(match.group(1))] = candidate
    return titles


def parse_season_page(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    titles = season_title_map(soup)
    records = []
    for heading in soup.find_all('h2'):
        match = re.fullmatch(r'Concierto\s+(\d+)', clean_text(heading), re.IGNORECASE)
        card = heading.find_parent('div', class_='col') if match else None
        if card is None:
            continue
        card_text = clean_text(card)
        event_date = parse_spanish_date(card_text, 2026)
        venue_match = re.search(r'Lugar:\s*(.+?)(?=\s+Clasificación:|$)', card_text, re.IGNORECASE)
        link = next(
            (candidate for candidate in card.find_all('a', href=True)
             if re.search(r'Comprar', clean_text(candidate), re.IGNORECASE)),
            None,
        )
        if not event_date or not venue_match or not link:
            continue
        number = int(match.group(1))
        description_node = heading.find_next('p')
        description = clean_text(description_node) or None
        title = titles.get(number) or f'Concierto {number} de temporada: Identidad'
        records.append(base_record(
            title, event_date, link['href'], None, venue_match.group(1).strip(), description
        ))
    return records


def parse_programme_page(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    records = []
    for month_heading in soup.select('h4.section-title'):
        month = clean_text(month_heading).lower()
        if month not in MONTHS:
            continue
        card = month_heading.find_parent('div', class_='col')
        day_heading = card.find('h3') if card else None
        title_heading = card.find('h2') if card else None
        venue_node = card.select_one('.event-tips-place') if card else None
        link = card.find('a', href=True) if card else None
        day_match = re.search(r'\b(\d{1,2})\b', clean_text(day_heading))
        if not all((day_match, title_heading, venue_node, link)):
            continue
        try:
            event_date = date(date.today().year, MONTHS[month], int(day_match.group(1))).isoformat()
        except ValueError:
            continue
        subtitle = clean_text(title_heading.find_previous('h4'))
        description = subtitle if subtitle and subtitle.lower() != month else None
        records.append(base_record(
            clean_text(title_heading), event_date, link['href'], parse_time(clean_text(card)),
            clean_text(venue_node), description,
        ))
    return records


def parse_inaugural_post(post):
    soup = BeautifulSoup(post['content']['rendered'], 'html.parser')
    text = clean_text(soup)
    event_date = parse_spanish_date(text, 2026)
    venue_match = re.search(r'\ben el (Teatro Metropolitano(?: José Gutiérrez Gómez)?)\b', text)
    if not event_date or not venue_match:
        return None
    title = clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser'))
    return base_record(
        title, event_date, post['link'], parse_time(text), venue_match.group(1), text,
    )


class FilarmedOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmed_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CO',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            programme = session.get(
                f'{API_URL}/pages', params={'slug': 'programacion'}, timeout=45
            )
            programme.raise_for_status()
            season = session.get(
                f'{API_URL}/pages', params={'slug': 'temporada-2026-filarmed'}, timeout=45
            )
            season.raise_for_status()
            search = session.get(
                f'{API_URL}/pages', params={'search': 'temporada', 'per_page': 100}, timeout=60
            )
            search.raise_for_status()
            inaugural = session.get(
                f'{API_URL}/posts',
                params={'slug': 'concierto-solidario-uraba-temporada-2026-filarmed'},
                timeout=45,
            )
            inaugural.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Filarmed programme',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        programme_pages = programme.json()
        season_pages = season.json()
        if not programme_pages or not season_pages:
            raise ValueError('Filarmed programme or season page was not returned by the API')

        records = parse_programme_page(programme_pages[0])
        records.extend(parse_season_page(season_pages[0]))
        inaugural_posts = inaugural.json()
        if inaugural_posts:
            record = parse_inaugural_post(inaugural_posts[0])
            if record:
                records.append(record)
        for page in search.json():
            title = clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser'))
            if re.search(r'^Concierto\s+N[.°º]?\s*[2-9]\s+de Temporada', title, re.IGNORECASE):
                record = parse_event_page(page)
                if record:
                    records.append(record)

        unique = {}
        for record in records:
            normalized_venue = re.sub(r'\W+', '', record['venue'].lower())
            if normalized_venue.startswith('teatrometropolitano'):
                normalized_venue = 'teatrometropolitano'
            key = (record['date'], normalized_venue)
            existing = unique.get(key)
            score = (
                bool(record['description']),
                len(record['description'] or ''),
                bool(record['time_from']),
                record['url'].startswith(SOURCE_URL),
            )
            existing_score = (
                bool(existing and existing['description']),
                len(existing['description'] or '') if existing else 0,
                bool(existing and existing['time_from']),
                bool(existing and existing['url'].startswith(SOURCE_URL)),
            )
            if existing is None or score > existing_score:
                unique[key] = record
        return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    FilarmedOrgCrawler().run()


if __name__ == '__main__':
    main()
