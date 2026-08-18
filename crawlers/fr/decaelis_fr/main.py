import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.decaelis.fr/'
SOURCE = 'Ensemble De Caelis'
UPCOMING_URL = urljoin(SOURCE_URL, 'agenda-a-venir/')
PAST_URL = urljoin(SOURCE_URL, 'agenda-passe/')

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

COUNTRY_MARKERS = {
    'suisse': 'CH',
    'espagne': 'ES',
    'belgique': 'BE',
    'allemagne': 'DE',
    'italie': 'IT',
    'royaume-uni': 'GB',
    'royaume uni': 'GB',
    'pays-bas': 'NL',
    'luxembourg': 'LU',
    'autriche': 'AT',
    'portugal': 'PT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    lines = [' '.join(line.replace('\xa0', ' ').replace('\u202f', ' ').split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    path = parts.path.rstrip('/') + '/'
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, '', ''))


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b', value.lower())
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    if not month:
        return None
    try:
        from datetime import date

        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h(?:\s*(\d{1,2}))?', value.lower())
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def parse_location(value):
    text = clean_text(value)
    if not text:
        return None, None
    city = re.sub(r'\s*\([^)]*\)\s*$', '', text.split(',', 1)[0]).strip()
    lowered = text.lower()
    country_code = next(
        (code for marker, code in COUNTRY_MARKERS.items() if marker in lowered),
        'FR',
    )
    return city or None, country_code


def metadata_rows(article):
    result = {}
    for row in article.select('.righthalf table tr'):
        icon = row.select_one('i')
        cells = row.select('td')
        if not icon or len(cells) < 2:
            continue
        classes = set(icon.get('class') or [])
        value = clean_text(cells[-1])
        if 'fa-calendar' in classes:
            result['date'] = value
        elif 'fa-clock' in classes:
            result['time'] = value
        elif 'fa-globe' in classes:
            result['location'] = value
        elif 'fa-arrow-down' in classes:
            result['venue'] = value
    return result


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.event .event-wrapper')
    if not article:
        return None

    title = clean_text(article.select_one('.event-boldtitle'))
    metadata = metadata_rows(article)
    event_date = parse_date(metadata.get('date', ''))
    time_from = parse_time(metadata.get('time', ''))
    city, country_code = parse_location(metadata.get('location', ''))
    venue = clean_text(metadata.get('venue'))

    description_container = article.select_one('.righthalf')
    if description_container:
        description_container = BeautifulSoup(str(description_container), 'html.parser')
        for unwanted in description_container.select(
            '.event-boldtitle, table, a.button, .vc_empty_space, script, style'
        ):
            unwanted.decompose()
        description = clean_text(description_container)
    else:
        description = ''

    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(url),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DecaelisFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='decaelis_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _get(self, url):
        response = requests.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return response.text

    def _archive_urls(self, start_url):
        page_url = start_url
        seen_pages = set()
        event_urls = []

        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            soup = BeautifulSoup(self._get(page_url), 'html.parser')
            event_urls.extend(
                canonical_url(link.get('href'))
                for link in soup.select('a.event-link[href*="/event/"]')
                if link.get('href')
            )
            next_link = next(
                (
                    link for link in soup.select('a[href]')
                    if 'next events' in clean_text(link).lower()
                ),
                None,
            )
            page_url = canonical_url(next_link.get('href')) if next_link else None

        return event_urls

    def _scrape_event(self, url):
        try:
            return parse_event(self._get(url), url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch De Caelis event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        urls = list(dict.fromkeys(self._archive_urls(UPCOMING_URL) + self._archive_urls(PAST_URL)))
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(self._scrape_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                record = future.result()
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete De Caelis event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, city, or country is missing',
                    )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    DecaelisFrCrawler().run()


if __name__ == '__main__':
    main()
