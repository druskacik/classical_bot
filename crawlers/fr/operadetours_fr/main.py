import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operadetours.fr/'
SOURCE = 'Opéra de Tours'
PROGRAM_URL = urljoin(SOURCE_URL, 'fr/programmation')
ARCHIVE_URL = urljoin(SOURCE_URL, 'fr/programmation/archives')
ARCHIVE_AJAX_URL = urljoin(
    SOURCE_URL,
    'fr/ssks/ajax/modspe/operadetours_liste_evenements_archives',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janv': 1, 'janvier': 1, 'févr': 2, 'fevr': 2, 'février': 2,
    'fevrier': 2, 'mars': 3, 'avr': 4, 'avril': 4, 'mai': 5,
    'juin': 6, 'juil': 7, 'juillet': 7, 'août': 8, 'aout': 8,
    'sept': 9, 'septembre': 9, 'oct': 10, 'octobre': 10,
    'nov': 11, 'novembre': 11, 'déc': 12, 'dec': 12, 'décembre': 12,
    'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def month_number(value):
    key = clean_text(value).casefold().rstrip('.')
    return MONTHS.get(key)


def canonical_event_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    query = parse_qs(parts.query)
    kept = urlencode({'nidseance': query['nidseance'][0]}) if query.get('nidseance') else ''
    return urlunsplit((parts.scheme, parts.netloc, parts.path, kept, ''))


def detail_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def listing_items(soup):
    items = []
    for element in soup.select('.liste-evt-item.evenement'):
        link = element.select_one('.liste-evt-texte a[href]')
        title = clean_text(element.select_one('.evt-titre'))
        day = clean_text(element.select_one('.evt-date-num'))
        month = month_number(element.select_one('.evt-date-mois'))
        raw_time = clean_text(element.select_one('.evt-date-time'))
        if not link or not title or not day.isdigit() or not month:
            continue
        match = re.search(r'(\d{1,2})\s*[Hh:]\s*(\d{2})', raw_time)
        items.append({
            'title': title,
            'day': int(day),
            'month': month,
            'time_from': f'{int(match.group(1)):02d}:{match.group(2)}' if match else None,
            'url': canonical_event_url(link.get('href')),
        })
    return items


def ajax_html(payload):
    fragments = []
    for command in payload:
        if isinstance(command.get('data'), str):
            fragments.append(command['data'])
        for argument in command.get('arguments') or []:
            if isinstance(argument, str) and '<' in argument:
                fragments.append(argument)
    return '\n'.join(fragments)


def archive_items(session):
    items = listing_items(get_soup(session, ARCHIVE_URL))
    page = 1
    while True:
        params = {
            'ajax': 1,
            'useAjax': 1,
            'page': page,
            'vue': 'liste',
            'uniqID': 'liste_evenements_archives',
            'order': 'date_ev.field_date_evenement_value/desc',
            'renderMode': 'addmore',
            'arguments_specifiques': json.dumps({
                'uniqid': 'liste_evenements_archives', 'nbElement': 24,
            }, separators=(',', ':')),
        }
        response = session.get(ARCHIVE_AJAX_URL, params=params, timeout=45)
        response.raise_for_status()
        page_items = listing_items(BeautifulSoup(ajax_html(response.json()), 'html.parser'))
        if not page_items:
            break
        items.extend(page_items)
        page += 1
    return items


def assign_years(items, descending):
    if not items:
        return []
    today = date.today()
    year = today.year
    previous_month = items[0]['month']
    # The first current-season item can be in the next calendar year; choose
    # the closest plausible occurrence before walking the ordered feed.
    if not descending and items[0]['month'] < today.month - 6:
        year += 1
    result = []
    for item in items:
        month = item['month']
        if descending and month > previous_month:
            year -= 1
        elif not descending and month < previous_month:
            year += 1
        previous_month = month
        try:
            item['date'] = date(year, month, item['day']).isoformat()
        except ValueError:
            continue
        result.append(item)
    return result


def detail_data(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, url)
    description_parts = []
    for selector in (
        '.node-evt-chapo', '.node-evt-programme', '.node-evt-descriptif',
        '.node-evt-distribution', '.node-evt-texte-suite',
    ):
        for element in soup.select(selector):
            text = clean_text(element)
            if text and text not in description_parts:
                description_parts.append(text)
    venues = {}
    for session_element in soup.select('.seance-header'):
        session_id = (session_element.get('id') or '').removeprefix('seance-num-')
        venue = clean_text(session_element.select_one('.js-bts-lieu-seance'))
        if session_id and venue:
            venues[session_id] = venue
    return '\n\n'.join(description_parts) or None, venues


def occurrence_venue(item, detail):
    _, venues = detail
    session_id = (parse_qs(urlsplit(item['url']).query).get('nidseance') or [None])[0]
    if session_id and venues.get(session_id):
        return venues[session_id]
    unique_venues = set(venues.values())
    if len(unique_venues) == 1:
        return unique_venues.pop()
    # This is a venue-specific calendar and pages without ticketing-session
    # venue data are presented as performances at its home theatre.
    if not unique_venues:
        return 'Grand Théâtre de Tours'
    return None


def venue_city(venue):
    normalized = clean_text(venue).casefold()
    if 'tours' in normalized or 'grand théâtre' in normalized or 'grand theatre' in normalized:
        return 'Tours'
    return None


class OperaDeToursCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operadetours_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        current = assign_years(listing_items(get_soup(session, PROGRAM_URL)), False)
        archived = assign_years(archive_items(session), True)
        occurrences = current + archived

        details = {}
        urls = sorted({detail_url(item['url']) for item in occurrences})
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(detail_data, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    details[url] = future.result()
                except (requests.RequestException, ValueError, TypeError) as error:
                    log_message(
                        'Failed to scrape Opéra de Tours event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for item in occurrences:
            detail = details.get(detail_url(item['url']), (None, {}))
            description, _ = detail
            venue = occurrence_venue(item, detail)
            city = venue_city(venue)
            if not venue or not city:
                continue
            records.append({
                'title': item['title'],
                'date': item['date'],
                'url': item['url'],
                'time_from': item['time_from'],
                'venue': venue,
                'city': city,
                'country_code': 'FR',
                'description': description,
            })
        return records


def main():
    return OperaDeToursCrawler().run()


if __name__ == '__main__':
    main()
