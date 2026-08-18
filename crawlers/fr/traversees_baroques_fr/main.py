import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.traversees-baroques.fr/'
AGENDA_URL = f'{SOURCE_URL}agenda/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Les Traversées Baroques'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'decembre': 12,
}

COUNTRY_CODES = {
    'allemagne': 'DE',
    'belgique': 'BE',
    'espagne': 'ES',
    'france': 'FR',
    'italie': 'IT',
    'luxembourg': 'LU',
    'pays-bas': 'NL',
    'pologne': 'PL',
    'république tchèque': 'CZ',
    'republique tcheque': 'CZ',
    'royaume-uni': 'GB',
    'suisse': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def parse_date(value):
    match = re.fullmatch(r'\s*(\d{1,2})\s+([a-zéûô]+)\s+(20\d{2})\s*', value.lower())
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def agenda_years(session):
    soup = BeautifulSoup(get_response(session, AGENDA_URL).text, 'html.parser')
    years = set()
    for option in soup.select('select option[value*="annee="]'):
        match = re.search(r'annee=(20\d{2})', option.get('value', ''))
        if match:
            years.add(int(match.group(1)))
    return sorted(years)


def agenda_items(session, year):
    response = get_response(session, AGENDA_URL, params={'annee': year})
    soup = BeautifulSoup(response.text, 'html.parser')
    items = []
    for article in soup.select('article.agenda_item[data-id]'):
        heading = article.select_one('h3')
        time_node = article.select_one('time.date')
        title = clean_text(heading)
        event_date = parse_date(clean_text(time_node))
        event_id = article.get('data-id', '').strip()
        if title and event_date and event_id:
            items.append({'id': event_id, 'title': title, 'date': event_date, 'year': year})
    return items


def detail_html(item):
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.post(
        AJAX_URL,
        data={'action': 'load_agenda_by_ajax', 'id': item['id']},
        headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': AGENDA_URL},
        timeout=45,
    )
    response.raise_for_status()
    return response.text


def location_parts(address):
    parts = [part.strip() for part in address.split(',') if part.strip()]
    if len(parts) < 3:
        return None
    country_code = COUNTRY_CODES.get(parts[-1].lower())
    if not country_code:
        return None
    venue = parts[0]
    city = parts[-2]
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def records_from_detail(item, html):
    soup = BeautifulSoup(html, 'html.parser')
    metadata = {}
    for row in soup.select('.metas tr'):
        cells = row.select('td')
        if len(cells) >= 2:
            metadata[clean_text(cells[0]).upper()] = clean_text(cells[1])

    location = location_parts(metadata.get('ADRESSE', ''))
    if not location:
        return []
    venue, city, country_code = location
    description = clean_text(soup.select_one('.content_free')) or None
    times = re.findall(r'(?<!\d)([01]?\d|2[0-3])h([0-5]\d)(?!\d)', metadata.get('HEURE', ''))
    start_times = list(dict.fromkeys(f'{int(hour):02d}:{minute}' for hour, minute in times)) or [None]
    event_url = f'{AGENDA_URL}?{urlencode({"annee": item["year"]})}#event-{item["id"]}'
    return [
        {
            'title': item['title'],
            'date': item['date'],
            'url': event_url,
            'time_from': start_time,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for start_time in start_times
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = []
    for year in agenda_years(session):
        items.extend(agenda_items(session, year))

    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(detail_html, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                records.extend(records_from_detail(item, future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Traversées Baroques event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=f'{AGENDA_URL}?annee={item["year"]}#event-{item["id"]}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class TraverseesBaroquesFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='traversees_baroques_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
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
    TraverseesBaroquesFrCrawler().run()


if __name__ == '__main__':
    main()
