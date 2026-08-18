import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestrevictorhugo.fr/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
SOURCE = 'Orchestre Victor Hugo'

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


def normalize(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).lower())
        if not unicodedata.combining(character)
    )


def parse_date_time(value):
    text = normalize(value)
    match = re.search(
        r'(\d{1,2})\s+([a-z]+)\s+(20\d{2})(?:\s*-\s*(\d{1,2})h(?:(\d{2}))?)?',
        text,
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        time_from = f'{int(match.group(4)):02d}:{int(match.group(5) or 0):02d}'
    return event_date, time_from


def parse_location(value):
    text = clean_text(value)
    if ' - ' not in text:
        return '', '', 'FR'
    venue, city = (part.strip() for part in text.rsplit(' - ', 1))
    country_code = 'FR'
    if re.search(r'\b(?:suisse|switzerland)\b', city, re.I):
        country_code = 'CH'
        city = re.sub(r'\s*\((?:Suisse|Switzerland)\)\s*$', '', city, flags=re.I)
    elif normalize(city) == 'freiburg':
        country_code = 'DE'
    return venue, city.strip(), country_code


def description_from_page(soup):
    parts = []
    about = clean_text(soup.select_one('.concert-about .content'))
    if about:
        parts.append(about)
    programme_heading = next(
        (heading for heading in soup.select('.concert-right-inner h2')
         if normalize(heading) == 'le programme'),
        None,
    )
    if programme_heading:
        programme = programme_heading.find_next_sibling(class_='content')
        programme_text = clean_text(programme)
        if programme_text:
            parts.append('Programme\n' + programme_text)
    return '\n\n'.join(parts) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    description = description_from_page(soup)
    records = []
    for occurrence in soup.select('.concert-dates .concert-date'):
        event_date, time_from = parse_date_time(occurrence.select_one('.concert-date-title'))
        venue, city, country_code = parse_location(
            occurrence.select_one('.concert-date-address')
        )
        if not title or not event_date or not venue or not city:
            log_message(
                'Skipped incomplete Orchestre Victor Hugo concert occurrence',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
                error_message='Required title, date, venue, or city is missing',
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class OrchestreVictorHugoFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestrevictorhugo_fr',
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
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = sorted({
            link.get('href')
            for link in soup.select('.concertitem-title[href]')
            if link.get('href')
        })

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Orchestre Victor Hugo concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue'], item['city']
            ),
        )


def main():
    OrchestreVictorHugoFrCrawler().run()


if __name__ == '__main__':
    main()
