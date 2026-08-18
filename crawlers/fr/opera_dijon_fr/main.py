import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera-dijon.fr/'
SOURCE = 'Opéra de Dijon'
CITY = 'Dijon'
CURRENT_CALENDAR = urljoin(
    SOURCE_URL, 'fr/au-programme/calendrier/saison-26-27/'
)
ARCHIVES_URL = urljoin(SOURCE_URL, 'fr/archives/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = (
        text.replace('\xa0', ' ')
        .replace('\u202f', ' ')
        .replace('\u200b', '')
        .replace('\xad', '')
    )
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_year(url):
    match = re.search(r'/saison-(\d{2})-(\d{2})(?:/|$)', urlparse(url).path)
    if not match:
        return None
    return 2000 + int(match.group(1))


def calendar_roots(session):
    home = get_soup(session, SOURCE_URL)
    current_link = home.select_one(
        'a[href*="/fr/au-programme/calendrier/saison-"]'
    )
    current = (
        urljoin(SOURCE_URL, current_link.get('href', ''))
        if current_link
        else CURRENT_CALENDAR
    )
    match = re.search(
        r'(/fr/au-programme/calendrier/saison-\d{2}-\d{2}/)',
        urlparse(current).path,
    )
    current = urljoin(SOURCE_URL, match.group(1).lstrip('/')) if match else CURRENT_CALENDAR
    roots = {current}

    # Older calendars move under /archives/. Probe a bounded history because
    # the archive landing page links only the newest archived season.
    current_year = season_year(current) or date.today().year
    for start_year in range(current_year - 1, current_year - 16, -1):
        short_year = start_year % 100
        archive = urljoin(
            ARCHIVES_URL, f'saison-{short_year:02d}-{(short_year + 1) % 100:02d}/'
        )
        response = session.get(archive, timeout=60)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        if BeautifulSoup(response.text, 'html.parser').select_one('.bloc_spectacle'):
            roots.add(archive)
    return sorted(roots, key=lambda value: season_year(value) or 0, reverse=True)


def detail_urls(session, root):
    found = set()
    page = 1
    while True:
        soup = get_soup(session, root, params={'mois': '', 'page': page})
        page_urls = {
            urljoin(root, link.get('href', ''))
            for link in soup.select('.bloc_spectacle h2 a[href]')
        }
        page_urls = {url for url in page_urls if season_year(url) is not None}
        new_urls = page_urls - found
        if not new_urls:
            break
        found.update(new_urls)
        page += 1
    return found


def parse_date(day_text, month_text, start_year):
    month = MONTHS.get(clean_text(month_text).lower())
    try:
        day = int(clean_text(day_text))
        year = start_year if month and month >= 7 else start_year + 1
        return date(year, month, day).isoformat()
    except (TypeError, ValueError):
        return None


def parse_detail(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('.partie_gauche > h1'))
    start_year = season_year(url)
    if not title or start_year is None:
        return []

    description = clean_text(soup.select_one('#paragraphes')) or None
    records = []
    for occurrence in soup.select('.bloc_dates'):
        event_date = parse_date(
            occurrence.select_one('.date_nombre'),
            occurrence.select_one('.mois'),
            start_year,
        )
        venue = clean_text(occurrence.select_one('.lieu_texte'))
        venue = re.sub(r'audit[oO]rium', 'Auditorium', venue)
        time_text = clean_text(occurrence.select_one('.horaires'))
        time_match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', time_text)
        if not event_date or not venue:
            continue
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': (
                    f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
                    if time_match
                    else None
                ),
                'venue': venue,
                'city': CITY,
                'country_code': 'FR',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = set()
    for root in calendar_roots(session):
        urls.update(detail_urls(session, root))

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(parse_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class OperaDijonFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_dijon_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaDijonFrCrawler().run()


if __name__ == '__main__':
    main()
