import html
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://theatromunicipal.org.br/'
SOURCE = 'Theatro Municipal de São Paulo'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/eventos'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'janeiro': 1,
    'fevereiro': 2,
    'marco': 3,
    'abril': 4,
    'maio': 5,
    'junho': 6,
    'julho': 7,
    'agosto': 8,
    'setembro': 9,
    'outubro': 10,
    'novembro': 11,
    'dezembro': 12,
}

DATE_RE = re.compile(
    r'^(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})$', re.IGNORECASE
)
TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})$')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character
        for character in unicodedata.normalize('NFD', value.casefold())
        if unicodedata.category(character) != 'Mn'
    )


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            ),
            pool_connections=16,
            pool_maxsize=16,
        ),
    )
    return session


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def event_catalogue(session):
    """Return every music event URL exposed by the public WordPress API."""
    page = 1
    events = []
    while True:
        response = get_response(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,class_list',
            },
        )
        payload = response.json()
        for item in payload:
            if 'categorias-eventos-musica' in (item.get('class_list') or []):
                url = item.get('link')
                if url:
                    events.append(url)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(events))


def parse_date(value):
    match = DATE_RE.fullmatch(normalized(clean_text(value)))
    if not match:
        return None
    day, month_name, year = match.groups()
    try:
        return datetime(int(year), MONTHS[month_name], int(day)).date().isoformat()
    except (KeyError, ValueError):
        return None


def event_description(soup):
    parts = []
    for section in soup.select('section'):
        headings = [clean_text(node) for node in section.select('h1, h2, h3')]
        if not any('descrição do evento' in heading.casefold() for heading in headings):
            continue
        for node in section.select('.elementor-widget-text-editor'):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)
    if parts:
        return '\n\n'.join(parts)

    # Older templates do not always wrap the description in the named section.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (json.JSONDecodeError, TypeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict):
                text = clean_text(value.get('description'))
                if text:
                    return text
    return None


def event_venue(info, title):
    for node in info.select('.elementor-icon-list-text'):
        text = clean_text(node)
        match = re.match(r'^Local de realização:\s*(.+)$', text, re.IGNORECASE)
        if match:
            venue = clean_text(match.group(1))
            if venue and normalized(venue) not in {'outros', 'evento externo'}:
                return venue

    # Some records only expose the broad location near the page title.
    for node in soup_icon_texts(info):
        match = re.match(r'^Local:\s*(.+)$', node, re.IGNORECASE)
        if match:
            venue = clean_text(match.group(1))
            if venue and normalized(venue) not in {'outros', 'evento externo'}:
                return venue

    # A few external events use only the generic location value in metadata,
    # while naming an unambiguous venue in the title (for example a museum).
    match = re.search(
        r'\b(?:no|na)\s+((?:Museu|Teatro|Theatro|Auditório|Sala)\s+[^|:]+)$',
        title,
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1))
    return None


def soup_icon_texts(node):
    return [clean_text(item) for item in node.select('.elementor-icon-list-text')]


def event_performances(info):
    values = [clean_text(node) for node in info.select('.jet-listing-dynamic-field__content')]
    performances = []
    pending_date = None
    for value in values:
        date = parse_date(value)
        if date:
            pending_date = date
            performances.append([date, None])
            continue
        time_match = TIME_RE.fullmatch(value)
        if time_match and pending_date and performances[-1][1] is None:
            hour, minute = map(int, time_match.groups())
            if hour <= 23 and minute <= 59:
                performances[-1][1] = f'{hour:02d}:{minute:02d}'
    return [tuple(item) for item in performances]


def parse_event(url, content):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('h1.elementor-heading-title'))
    info = soup.select_one('#infos')
    if not title or not info:
        return []

    venue = event_venue(info, title)
    if not venue:
        return []
    description = event_description(soup)
    return [
        {
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            # The theatre's programme and its external-event locations are a
            # São Paulo municipal calendar; tours are not published here.
            'city': 'São Paulo',
            'description': description,
        }
        for date, time_from in event_performances(info)
    ]


def get_concerts():
    session = build_session()
    urls = event_catalogue(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                parsed = parse_event(url, future.result().content)
                if not parsed:
                    log_message(
                        'Skipping music event with incomplete date or venue',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
                records.extend(parsed)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class TheatroMunicipalOrgBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theatromunicipal_org_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheatroMunicipalOrgBrCrawler().run()


if __name__ == '__main__':
    main()
