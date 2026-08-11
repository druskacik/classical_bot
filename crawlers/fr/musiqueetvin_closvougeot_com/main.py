import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musiqueetvin-closvougeot.com/'
SOURCE = 'Musique & Vin au Clos Vougeot'
FESTIVAL_URL = urljoin(SOURCE_URL, 'le-festival/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5, 'juin': 6,
    'juillet': 7, 'aout': 8, 'septembre': 9, 'octobre': 10,
    'novembre': 11, 'decembre': 12,
}

VENUE_CITIES = {
    'abbaye de saint-vivant': 'Curtil-Vergy',
    'chateau de meursault': 'Meursault',
    'chateau du clos de vougeot': 'Vougeot',
    'halles de beaune': 'Beaune',
    'lanterne magique': 'Beaune',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', value)
    return ''.join(char for char in value if not unicodedata.combining(char)).lower()


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+'
        r'(janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre)'
        r'\s+(20\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    month = MONTHS.get(normalized(match.group(2)))
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*[hH](?:\s*([0-5]\d))?\b', value)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def city_for_venue(venue):
    venue_key = normalized(venue).replace('clos de vougeot', 'clos de vougeot')
    for known_venue, city in VENUE_CITIES.items():
        if known_venue in venue_key:
            return city
    return None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_block = soup.select_one('.bloc-titre')
    time_block = soup.select_one('.bloc-time')
    venue = clean_text(title_block.select_one('h3')) if title_block else ''
    event_date = parse_date(f'{url} {clean_text(soup.title)}')
    city = city_for_venue(venue)

    time_text = clean_text(time_block)
    performance_label = next(
        (
            label for label in time_block.select('strong')
            if re.search(r'concert|r[eé]cital', clean_text(label), re.IGNORECASE)
        ),
        None,
    ) if time_block else None
    title = clean_text(performance_label)
    time_node = performance_label.find_previous('em') if performance_label else None
    performance_time = parse_time(clean_text(time_node)) or parse_time(time_text)

    description_parts = []
    supplementary = soup.select_one('.txt-sup')
    if supplementary and clean_text(supplementary):
        description_parts.append(clean_text(supplementary))
    for block in soup.select('.col-3.txt'):
        text = clean_text(block)
        if text:
            description_parts.append(text)
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None

    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': performance_time,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description,
    }


class MusiqueEtVinClosVougeotComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiqueetvin_closvougeot_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(FESTIVAL_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Musique & Vin festival page',
                event='crawler_fetch_failed',
                level='error',
                url=FESTIVAL_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        event_urls = list(dict.fromkeys(
            urljoin(FESTIVAL_URL, link['href'])
            for link in soup.select('.event a[href]')
        ))
        if not event_urls:
            raise ValueError('No event links found on the festival page')

        records = []
        for url in event_urls:
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Musique & Vin event',
                    event='crawler_event_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_event(detail.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Musique & Vin event',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                )
        return records


def main():
    return MusiqueEtVinClosVougeotComCrawler().run()


if __name__ == '__main__':
    main()
