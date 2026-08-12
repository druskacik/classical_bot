import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fvgorchestra.it/'
SOURCE = 'FVG Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/etn'
CONCERT_CATEGORY_ID = 21

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

PROVINCE_CITIES = {
    'GO': 'Gorizia',
    'PN': 'Pordenone',
    'TS': 'Trieste',
}

KNOWN_CITIES = (
    'Gemona del Friuli', 'Feletto Umberto', 'San Vito al Tagliamento',
    'Cividale del Friuli', 'Lignano Sabbiadoro', 'Gradisca d’Isonzo',
    "Gradisca d'Isonzo", 'Cervignano del Friuli', 'Monfalcone',
    'Pordenone', 'Palmanova', 'Codroipo', 'Tolmezzo', 'Sacile',
    'Gorizia', 'Trieste', 'Udine', 'Milano', 'Venezia', 'Treviso',
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(
        r'\b(Gennaio|Febbraio|Marzo|Aprile|Maggio|Giugno|Luglio|Agosto|'
        r'Settembre|Ottobre|Novembre|Dicembre)\s+(\d{1,2}),\s*(20\d{2})\b',
        value,
        re.I,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(1).casefold()], int(match.group(2))
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*([ap]m)\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).casefold() == 'pm':
        hour += 12
    if hour > 23 or int(match.group(2)) > 59:
        return None
    return f'{hour:02d}:{match.group(2)}'


def parse_meta(soup):
    values = {}
    container = soup.select_one('.etn-event-meta-info')
    if not container:
        return values
    for item in container.select('li'):
        label_node = item.select_one('span')
        if label_node is None:
            continue
        label = clean_text(label_node).rstrip(':').strip().casefold()
        label_node.extract()
        values[label] = clean_text(item)
    return values


def extract_city(venue, title):
    evidence = f'{venue}\n{title}'
    for city in KNOWN_CITIES:
        if re.search(rf'(?<!\w){re.escape(city)}(?!\w)', evidence, re.I):
            return city.replace("'", '’')

    province = re.search(r'\((GO|PN|TS)\)\s*$', venue, re.I)
    if province:
        return PROVINCE_CITIES[province.group(1).upper()]

    # Venue strings on this source commonly end in "di <city>". Restrict the
    # capture to a short proper-name phrase so addresses and prose cannot leak
    # into the city field.
    match = re.search(
        r'\bdi\s+([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ’\' -]{1,45})(?:\s*\([A-Z]{2}\))?$',
        venue,
    )
    if match:
        return match.group(1).strip()

    suffix = re.search(r'[–—-]\s*([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ’\' ]{1,40})$', title)
    if suffix:
        return suffix.group(1).strip()
    return None


def parse_event(item, soup):
    title = clean_text(item.get('title', {}).get('rendered'))
    url = item.get('link', '').strip()
    meta = parse_meta(soup)
    event_date = parse_date(meta.get('date', ''))
    venue = meta.get('venue', '').strip()
    city = extract_city(venue, title) if venue else None
    if not title or not url or not event_date or not venue or not city:
        return None

    content_html = item.get('content', {}).get('rendered', '')
    description = clean_text(BeautifulSoup(content_html, 'html.parser')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(meta.get('time', '')),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FvgorchestraItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fvgorchestra_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'etn_category': CONCERT_CATEGORY_ID,
                        'per_page': 100,
                        'page': page,
                    },
                    timeout=45,
                )
                response.raise_for_status()
                items.extend(response.json())
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch FVG Orchestra concert API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            page += 1

        records = []
        for item in items:
            url = item.get('link', '')
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event(item, BeautifulSoup(response.content, 'html.parser'))
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped FVG Orchestra event with incomplete required fields',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch FVG Orchestra event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FvgorchestraItCrawler().run()


if __name__ == '__main__':
    main()
