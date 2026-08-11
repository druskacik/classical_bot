import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-massy.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'fr/spectacles/copy-contact.html')
SOURCE = 'Opéra de Massy'
DEFAULT_VENUE = 'Opéra de Massy'
DEFAULT_CITY = 'Massy'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3,
    'avril': 4, 'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'aout': 8, 'septembre': 9, 'octobre': 10, 'novembre': 11,
    'décembre': 12, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_event_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    query = urlencode([
        (key, item) for key, item in parse_qsl(parts.query)
        if key in {'cmp_id', 'news_id', 'vID'}
    ])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ''))


def parse_datetime(value):
    match = re.fullmatch(
        r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\s*-\s*(\d{1,2}):(\d{2})',
        clean_text(value),
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    return event_date, f'{int(match.group(4)):02d}:{match.group(5)}'


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for link in soup.select('.all_futur_event .evt > a[href*="news_id="]'):
        title = clean_text(link.select_one('h4'))
        event_date, time_from = parse_datetime(link.select_one('.date'))
        url = canonical_event_url(link.get('href'))
        if not title or not event_date or not url:
            log_message(
                'Skipped incomplete Opéra de Massy calendar entry',
                event='crawler_item_skipped',
                level='warning',
                url=url or CALENDAR_URL,
                error_type='IncompleteEventData',
                error_message='Required title, date, or URL is missing',
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': DEFAULT_VENUE,
            'city': DEFAULT_CITY,
            'country_code': 'FR',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    candidates = soup.select('.container.main-content .twelve.columns')
    if not candidates:
        return None
    content = max(candidates, key=lambda element: len(clean_text(element)))
    for element in content.select('script, style, .about-project, img, .flexslider'):
        element.decompose()
    return clean_text(content) or None


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail_description(response.text)


class OperaMassyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_massy_com',
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

    def scrape(self):
        response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = parse_calendar(response.text)

        descriptions = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_description, url): url
                for url in sorted({record['url'] for record in records})
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Opéra de Massy event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    OperaMassyComCrawler().run()


if __name__ == '__main__':
    main()
