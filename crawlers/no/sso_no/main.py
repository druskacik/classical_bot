import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sso.no/'
SOURCE = 'Stavanger Symfoniorkester'
CURRENT_URL = f'{SOURCE_URL}konsert/'
ARCHIVE_URL = f'{SOURCE_URL}konsertarkiv/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}

# The regional calendar sometimes gives only a named venue. These names are
# first-party location evidence, rather than a home-city fallback for tours.
REGIONAL_VENUES = {
    'jørpeland kirke': 'Jørpeland',
    'vår frelsers kirke': 'Haugesund',
    'riska kirke': 'Sandnes',
    'nærbø kyrkje': 'Nærbø',
    'sandnes kulturhus': 'Sandnes',
    'suldal kulturhus': 'Sand',
    'tungenes fyr': 'Randaberg',
    'utstein kloster': 'Mosterøy',
    'hå gamle prestegard': 'Nærbø',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def embedded_json(html, variable):
    """Read a JSON array embedded in the site's petite-vue configuration."""
    marker = f'{variable}:'
    start = html.find(marker)
    if start < 0:
        return []
    start = html.find('[', start + len(marker))
    if start < 0:
        return []

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth == 0:
                return json.loads(html[start:index + 1])
    return []


def occurrences(event):
    values = []
    details = event.get('concert_showings_details') or []
    for showing in details:
        value = showing.get('date_content')
        if value:
            values.append(value)
    if not values:
        for showing in event.get('concert_showings') or []:
            timestamp = showing.get('showing_date')
            if timestamp:
                values.append(datetime.fromtimestamp(timestamp).strftime('%Y-%m-%dT%H:%M'))
    return list(dict.fromkeys(values))


def city_for_venue(venue, is_regional):
    folded = venue.casefold()
    for marker, city in REGIONAL_VENUES.items():
        if marker in folded:
            return city
    if 'stavanger konserthus' in folded or any(name in folded for name in (
        'fartein valen', 'zetlitz', 'kuppelhallen', 'bjergsted',
        'stavanger domkirke', 'st. petri kirke', 'st petri kirke',
    )):
        return 'Stavanger'
    # Touring venues often state the municipality after a comma.
    parts = [part.strip() for part in venue.split(',') if part.strip()]
    if is_regional and len(parts) > 1 and re.fullmatch(r'[A-Za-zÆØÅæøå -]+', parts[-1]):
        return parts[-1]
    if not is_regional:
        return 'Stavanger'
    return None


def venue_map(soup):
    result = {}
    for showing in soup.select('.concert-program-wrapper .concert-showing'):
        date_text = clean_text(showing.select_one('.concert-showing__date'))
        venue = clean_text(showing.select_one('.concert-showings__venue'))
        match = re.search(r'(\d{1,2})\.\s+[A-Za-zÆØÅæøå]+(?:\s+\d{4})?', date_text)
        if match and venue:
            result[int(match.group(1))] = venue

    program = soup.select_one('.concert-program-wrapper')
    lines = clean_text(program).splitlines() if program else []
    try:
        places_index = next(i for i, line in enumerate(lines) if line.casefold() == 'spillesteder')
    except StopIteration:
        places_index = -1
    if places_index >= 0:
        for line in lines[places_index + 1:]:
            match = re.match(r'.*?\b(\d{1,2})\.\s+[^:]+:\s*(.+)$', line)
            if match:
                result[int(match.group(1))] = match.group(2).strip()

    default = None
    date_heading = next((i for i, line in enumerate(lines) if line.casefold() == 'datoer'), -1)
    candidates = lines[:date_heading] if date_heading > 0 else []
    for line in reversed(candidates):
        if re.search(r'konserthus|kirke|kyrkje|kulturhus|kloster|domkirke|fyr|prestegard', line, re.I):
            default = line
            break
    return result, default


def parse_event(html, event):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    url = event.get('permalink')
    if not title or not url:
        return []

    description_parts = []
    for selector in ('.concert__main__left', '.concert-program-wrapper'):
        value = clean_text(soup.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)
    description = '\n\n'.join(description_parts) or None

    categories = set(event.get('concert_categories') or [])
    is_regional = 1350 in categories
    venues, default_venue = venue_map(soup)
    records = []
    for value in occurrences(event):
        try:
            occurrence = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        venue = venues.get(occurrence.day) or default_venue
        if not venue:
            continue
        city = city_for_venue(venue, is_regional)
        if not city:
            continue
        records.append({
            'title': title,
            'date': occurrence.date().isoformat(),
            'url': url,
            # The calendar serializes date-only listings as midnight.
            'time_from': None if occurrence.strftime('%H:%M') == '00:00' else occurrence.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class SsoNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sso_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount('https://', HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
            ),
        ))
        events = {}
        for index_url in (CURRENT_URL, ARCHIVE_URL):
            response = session.get(index_url, timeout=60)
            response.raise_for_status()
            for event in embedded_json(response.text, 'concertsList'):
                if event.get('permalink'):
                    events[event['permalink']] = event

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(session.get, url, timeout=45): event
                for url, event in events.items()
            }
            for future in as_completed(futures):
                event = futures[future]
                url = event['permalink']
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_event(response.text, event))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to parse SSO concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    return SsoNoCrawler().run()


if __name__ == '__main__':
    main()
