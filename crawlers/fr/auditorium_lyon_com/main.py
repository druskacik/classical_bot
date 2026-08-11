import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.auditorium-lyon.com/fr'
PROGRAMME_URL = f'{SOURCE_URL}/programmation'
SOURCE = 'Auditorium - Orchestre national de Lyon'
CONCERT_TYPE = '126'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'jan': 1, 'fév': 2, 'fev': 2, 'mar': 3, 'avr': 4, 'mai': 5, 'juin': 6,
    'juil': 7, 'aoû': 8, 'aou': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'déc': 12, 'dec': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    return '\n'.join(line.strip() for line in text.replace('\xa0', ' ').splitlines() if line.strip())


def get_json_events(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = data.get('@graph', []) if isinstance(data, dict) else []
        if isinstance(data, dict) and data.get('@type') == 'Event':
            nodes = [data]
        events.extend(node for node in nodes if isinstance(node, dict) and node.get('@type') == 'Event')
    return events


def detail_description(soup):
    article = soup.select_one('.main-content article.wysiwyg')
    if not article:
        return None
    for unwanted in article.select('#accordeon_distribution, [id*="distribution"]'):
        parent = unwanted.find_parent(class_='accordeon')
        (parent or unwanted).decompose()
    return clean_text(article) or None


def detail_occurrences(soup):
    occurrences = []
    venue = None
    for term in soup.select('.Aside-section-infos dt'):
        label = clean_text(term).lower()
        values = []
        node = term.find_next_sibling()
        while node and node.name != 'dt':
            if node.name == 'dd':
                values.append(clean_text(node))
            node = node.find_next_sibling()
        if label.startswith('lieu') and values:
            venue = values[0]
        if label.startswith('date'):
            for value in values:
                matches = re.finditer(
                    r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\.?\s+(20\d{2})(?:\s+à\s+(\d{1,2})h(\d{2})?)?',
                    value,
                )
                for match in matches:
                    month = MONTHS.get(match.group(2).lower()[:3])
                    if not month:
                        continue
                    try:
                        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
                    except ValueError:
                        continue
                    time_from = None
                    if match.group(4):
                        time_from = f'{int(match.group(4)):02d}:{match.group(5) or "00"}'
                    occurrences.append((event_date, time_from))
    return occurrences, venue


def parse_detail(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    description = detail_description(soup)
    explicit_occurrences, explicit_venue = detail_occurrences(soup)
    records = []
    for event in get_json_events(soup):
        start = event.get('startDate') or ''
        event_date = start[:10]
        time_from = start[11:16] if len(start) >= 16 else None
        location = event.get('location') or {}
        address = location.get('address') or {}
        title = clean_text(event.get('name'))
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        country = clean_text(address.get('addressCountry')) or 'FR'
        if not (title and len(event_date) == 10 and venue and city):
            continue
        for occurrence_date, occurrence_time in explicit_occurrences or [(event_date, time_from)]:
            records.append({
                'title': title,
                'date': occurrence_date,
                'url': url,
                'time_from': occurrence_time,
                'venue': explicit_venue or venue,
                'city': city,
                'country_code': country.upper(),
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class AuditoriumLyonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='auditorium_lyon_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def _get(self, url, params=None):
        response = requests.get(url, params=params, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return response

    def _season_ids(self):
        soup = BeautifulSoup(self._get(PROGRAMME_URL).text, 'html.parser')
        values = {
            button.get('data-value')
            for button in soup.select('.saison-select [data-value]')
            if button.get('data-value')
        }
        for link in soup.select('a[href*="saison="]'):
            values.update(parse_qs(urlsplit(link.get('href')).query).get('saison', []))
        return sorted(values)

    def _detail_urls(self, season):
        urls = set()
        first = BeautifulSoup(
            self._get(
                PROGRAMME_URL,
                params={'type': CONCERT_TYPE, 'saison': season, 'page': 0},
            ).text,
            'html.parser',
        )
        pages = {0}
        for link in first.select('a[href*="page="]'):
            for value in parse_qs(urlsplit(link.get('href')).query).get('page', []):
                if value.isdigit():
                    pages.add(int(value))
        for page in range(max(pages) + 1):
            soup = first if page == 0 else BeautifulSoup(
                self._get(
                    PROGRAMME_URL,
                    params={'type': CONCERT_TYPE, 'saison': season, 'page': page},
                ).text,
                'html.parser',
            )
            page_urls = {
                urljoin(SOURCE_URL, link.get('href'))
                for link in soup.select('article.event a[href]')
            }
            urls.update(page_urls)
        return urls

    def scrape(self):
        urls = set()
        for season in self._season_ids():
            urls.update(self._detail_urls(season))

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(self._get, url): url for url in sorted(urls)}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    parsed = parse_detail(url, future.result().text)
                    if not parsed:
                        log_message(
                            'Skipped auditorium event without complete structured occurrence data',
                            event='crawler_item_skipped', level='warning', url=url,
                            error_type='IncompleteEventData',
                            error_message='No JSON-LD occurrence with title, date, venue, and city',
                        )
                    records.extend(parsed)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape auditorium event detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    AuditoriumLyonComCrawler().run()


if __name__ == '__main__':
    main()
