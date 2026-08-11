import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festival-colmar.com/fr/'
SOURCE = 'Festival International de Colmar'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

FRENCH_MONTHS = {
    'janvier': 1, 'fevrier': 2, 'février': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'aout': 8, 'août': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'decembre': 12,
    'décembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    url = urljoin(SOURCE_URL, value or '')
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\s+(\d{1,2}):(\d{2})',
        clean_text(value),
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = FRENCH_MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_datetime = datetime(
            int(match.group(3)), month, int(match.group(1)),
            int(match.group(4)), int(match.group(5)),
        )
    except ValueError:
        return None, None
    return event_datetime.date().isoformat(), event_datetime.strftime('%H:%M')


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('.fichespectacle')
    if not event:
        return None

    title = clean_text(event.select_one('.pos-title'))
    event_date, time_from = parse_datetime(event.select_one('.date'))

    address = event.select_one('.adresse')
    address_parts = list(address.stripped_strings) if address else []
    venue = clean_text(address_parts[0]) if address_parts else ''
    city = ''
    for part in reversed(address_parts[1:]):
        match = re.search(r'\b\d{5}\s+(.+)$', clean_text(part))
        if match:
            city = match.group(1).strip()
            break

    description_parts = []
    artists = clean_text(event.select_one('.pos-artiste'))
    if artists:
        description_parts.append('Artistes\n' + artists)
    for section in event.select('.pos-content'):
        if section.select_one('img'):
            continue
        text = clean_text(section)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class FestivalColmarComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festival_colmar_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        programme_urls = []
        for link in soup.select('a[href*="programme-edition-"]'):
            url = canonical_url(link.get('href'))
            parts = urlsplit(url)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
            if re.search(r'/programme-edition-20\d{2}$', parts.path) and url not in programme_urls:
                programme_urls.append(url)

        urls = []
        for programme_url in programme_urls:
            programme_response = requests.get(programme_url, headers=HEADERS, timeout=45)
            if programme_response.status_code == 404:
                continue
            programme_response.raise_for_status()
            programme = BeautifulSoup(programme_response.text, 'html.parser')
            for card in programme.select('article.spectacle'):
                link = card.select_one('a.uk-button[href*="/component/zoo/item/"]')
                if link:
                    url = canonical_url(link.get('href'))
                    if url not in urls:
                        urls.append(url)

        records = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Festival International de Colmar event',
                            event='crawler_item_skipped',
                            level='warning',
                            url=url,
                            error_type='IncompleteEventData',
                            error_message='Required title, date, URL, venue, or city is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Festival International de Colmar event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    FestivalColmarComCrawler().run()


if __name__ == '__main__':
    main()
