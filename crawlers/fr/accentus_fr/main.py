import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.accentus.fr/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'accentus'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janv': 1, 'janvier': 1, 'févr': 2, 'fevr': 2, 'février': 2,
    'fevrier': 2, 'mars': 3, 'avr': 4, 'avril': 4,
    'mai': 5, 'juin': 6, 'juil': 7, 'juillet': 7, 'août': 8, 'aout': 8,
    'sept': 9, 'septembre': 9, 'oct': 10, 'octobre': 10,
    'nov': 11, 'novembre': 11, 'déc': 12, 'dec': 12,
    'décembre': 12, 'decembre': 12,
}

FOREIGN_COUNTRIES = {
    'amsterdam': 'NL',
    'budapest': 'HU',
    'cuenca': 'ES',
    'melk': 'AT',
    'porto': 'PT',
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
    url = urljoin(SOURCE_URL, clean_text(value))
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\.?\s+(20\d{2})', clean_text(value))
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower().rstrip('.'))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(card):
    location = card.select_one(':scope > .ville')
    parts = list(location.stripped_strings) if location else []
    if len(parts) < 2:
        return '', '', 'FR'
    city = clean_text(parts[0])
    venue = clean_text(' '.join(parts[1:]))
    country_code = FOREIGN_COUNTRIES.get(city.split(' (', 1)[0].lower(), 'FR')
    city = re.sub(r'\s*\([^)]*(?:Autriche|Espagne|Hongrie|Pays-Bas|Portugal)\)\s*$', '', city)
    venue = re.sub(r'\s*\((?:Autriche|Espagne|Hongrie|Pays-Bas|Portugal)\)\s*$', '', venue)
    return city.strip(), venue.strip(), country_code


def parse_card(card):
    title_element = card.select_one('.titleContainer > div')
    event_date = parse_date(card.select_one(':scope > .date'))
    url = canonical_url(card.get('data-url'))
    city, venue, country_code = parse_location(card)
    time_from = clean_text(card.select_one('.mobiledetails .time')) or None
    if time_from and not re.fullmatch(r'\d{2}:\d{2}', time_from):
        time_from = None
    summary_element = card.select_one('.mobiledetails .details em')
    title = clean_text(title_element)
    if not title or not event_date or not url or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(summary_element) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    programmes = []
    for programme in soup.select('.concert_detail .programmes .programme'):
        composer = clean_text(programme.select_one('.artiste'))
        works = clean_text(programme.select_one('.desc'))
        text = ': '.join(part for part in (composer, works) if part)
        if text and text not in programmes:
            programmes.append(text)
    if programmes:
        parts.append('Programme\n' + '\n'.join(programmes))
    description = clean_text(soup.select_one('.concert_detail .right > .desc'))
    if description:
        parts.append(description)
    return '\n\n'.join(parts) or None


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail_description(response.text)


class AccentusFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='accentus_fr',
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
        response = requests.get(AGENDA_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        agenda_pages = {AGENDA_URL: response.text}
        soup = BeautifulSoup(response.text, 'html.parser')
        for link in soup.select('.saison_header a[href^="/agenda/"]'):
            url = urljoin(SOURCE_URL, link.get('href'))
            archive_response = requests.get(url, headers=HEADERS, timeout=45)
            archive_response.raise_for_status()
            agenda_pages[url] = archive_response.text

        records = []
        for page_url, html in agenda_pages.items():
            page = BeautifulSoup(html, 'html.parser')
            for card in page.select('.agenda-concert'):
                record = parse_card(card)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete accentus concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=canonical_url(card.get('data-url')) or page_url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )

        descriptions = {}
        urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_description, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape accentus concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url']) or record['description']
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    AccentusFrCrawler().run()


if __name__ == '__main__':
    main()
