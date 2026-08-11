import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.choregies.fr/'
SOURCE = "Les Chorégies d'Orange"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFD', clean_text(value).lower())
        if unicodedata.category(character) != 'Mn'
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_date_and_time(value, year):
    text = normalized(value)
    match = re.search(
        r'\b(\d{1,2})\s+(janvier|fevrier|mars|avril|mai|juin|juillet|aout|'
        r'septembre|octobre|novembre|decembre)\b',
        text,
    )
    if not match:
        return None, None
    try:
        event_date = date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)\b', text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, time_from


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.item-spectacle-description')
    heading = content.select_one('h1') if content else None
    if not content or not heading:
        return None

    title_element = heading.select_one('strong em')
    date_element = heading.select_one('div')
    title = clean_text(title_element)
    year_match = re.search(r'/programme--(20\d{2})-', url)
    event_date, time_from = parse_date_and_time(
        date_element,
        int(year_match.group(1)) if year_match else date.today().year,
    )

    venue = ''
    for element in content.select('p'):
        text = clean_text(element)
        if normalized(text) in {'theatre antique', "theatre antique d'orange"}:
            venue = text
            break

    description_node = BeautifulSoup(str(content), 'html.parser')
    description_heading = description_node.select_one('h1')
    if description_heading:
        description_heading.decompose()
    description = clean_text(description_node) or None

    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Orange',
        'country_code': 'FR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class ChoregiesFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='choregies_fr',
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
        urls = sorted({
            canonical_url(link.get('href'))
            for link in soup.select('.item-spectacle a[href*="programme--"]')
            if '--fr.html' in link.get('href', '')
        })

        records = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Chorégies programme detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Chorégies programme item',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, displayed date, or venue is missing',
                    )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    ChoregiesFrCrawler().run()


if __name__ == '__main__':
    main()
