import html
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.osba.art.br/'
EVENTS_URL = f'{SOURCE_URL}eventos/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Orquestra Sinfônica da Bahia (OSBA)'
ARCHIVE_START_YEAR = 2023

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

AJAX_HEADERS = {
    **HEADERS,
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'X-Requested-With': 'XMLHttpRequest',
    'Sec-CH-UA': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'Sec-CH-UA-Mobile': '?0',
    'Sec-CH-UA-Platform': '"Linux"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
    'Referer': EVENTS_URL,
}

# Parenthetical labels used for Salvador districts or venue subdivisions must
# not be mistaken for touring cities.
SALVADOR_AREAS = {
    'campo grande', 'canela', 'centro', 'centro historico', 'comercio',
    'carmo', 'garcia', 'graca', 'itapua', 'ondina', 'pelourinho',
    'rio vermelho', 'vitoria',
}


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', clean_text(value))
    return ''.join(char for char in value if not unicodedata.combining(char)).lower()


def get_catalogue(session):
    records = []
    for year in range(ARCHIVE_START_YEAR, date.today().year + 1):
        response = session.post(
            AJAX_URL,
            data=f'action=load_events_year&year={year}&page=1',
            headers=AJAX_HEADERS,
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get('events', []):
            event_date = item.get('date')
            url = item.get('link')
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(event_date or '')) and url:
                records.append({'date': event_date, 'url': url})
    return records


def parse_time(value):
    match = re.search(r'\b(\d{1,2})h(?:(\d{2}))?\b', clean_text(value), re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def city_from_venue(venue, title):
    if 'osba na estrada' in normalized(title):
        parenthetical = re.search(r'\(([^()]*)\)\s*$', venue)
        if parenthetical:
            return clean_text(parenthetical.group(1))
        district = re.search(r'\bdistrito\s+de\s+([^,()]+)', venue, re.I)
        if district:
            return clean_text(district.group(1))
        after_em = re.search(r'\bem\s+([^,()]+)', venue, re.I)
        if after_em:
            return clean_text(after_em.group(1))
        title_city = re.search(r'osba\s+na\s+estrada\s*\(([^()]*)\)', title, re.I)
        if title_city:
            return clean_text(title_city.group(1))
        if '|' in title:
            return clean_text(title.rsplit('|', 1)[-1])
        title_after_em = re.search(r'\bem\s+([^|–-]+)$', title, re.I)
        if title_after_em:
            return clean_text(title_after_em.group(1))
        after_dash = re.search(r'\s[–-]\s*([^–-]+)$', title)
        if after_dash:
            return re.sub(r'\s*\(\d{2}/\d{2}/\d{4}\)\s*$', '', clean_text(after_dash.group(1)))
        if not re.search(r'\b(teatro|igreja|museu|sala|auditorio|praca|parque)\b', normalized(venue)):
            return venue
    return 'Salvador'


def parse_event(item, session):
    response = session.get(item['url'], headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    article = soup.select_one('article.event-article')
    title_node = soup.select_one('h2.event-title')
    venue_node = soup.select_one('.ev-text-container h3.ev-title')
    time_node = soup.select_one('.ev-time-block')
    description_node = soup.select_one('.event-description')

    title = clean_text(title_node)
    venue = clean_text(venue_node)
    if not article or not title or not venue or normalized(title).startswith('cancelado'):
        return None

    city = city_from_venue(venue, title)
    description = clean_text(description_node) or None
    return {
        'title': title,
        'date': item['date'],
        'url': item['url'],
        'time_from': parse_time(time_node),
        'venue': venue,
        'city': city,
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET', 'POST'),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    catalogue = get_catalogue(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event, item, session): item for item in catalogue}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped an incomplete OSBA event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=item['url'],
                    )
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape OSBA event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OsbaArtBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osba_art_br',
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
    OsbaArtBrCrawler().run()


if __name__ == '__main__':
    main()
