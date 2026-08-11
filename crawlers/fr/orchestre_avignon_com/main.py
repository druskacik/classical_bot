import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestre-avignon.com/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-concerts-1.xml'
SOURCE = 'Orchestre national Avignon-Provence'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
}

DATE_RE = re.compile(
    r'(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+'
    r'(\d{1,2})\s+([a-zûé]+)\s+(20\d{2})(?:\s+(\d{1,2}):(\d{2}))?',
    re.IGNORECASE,
)

# These venue names are first-party evidence of an Avignon location even when
# the detail page omits a separate city field.
AVIGNON_VENUES = (
    'avignon', 'opéra grand avignon', "la fabrica du festival d'avignon",
    'bibliothèque ceccano', 'théâtre des halles', 'le delirium',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def sitemap_urls(xml):
    urls = [unescape(value) for value in re.findall(r'<loc>(.*?)</loc>', xml)]
    return [
        url for url in urls
        if '/concerts/' in url and url.rstrip('/') != f'{SOURCE_URL}concerts'
    ]


def parse_location(value, title):
    venue = clean_text(value)
    if ' / ' in venue:
        venue, city = [part.strip() for part in venue.rsplit(' / ', 1)]
        return venue, city
    if ',' in venue:
        possible_venue, possible_city = [part.strip() for part in venue.rsplit(',', 1)]
        if possible_city and len(possible_city.split()) <= 4:
            return possible_venue, possible_city

    lower_venue = venue.lower().replace('’', "'")
    if any(marker in lower_venue for marker in AVIGNON_VENUES):
        return venue, 'Avignon'

    # Touring titles frequently provide the missing city in parentheses or
    # after a dash. Only use a short final place-like fragment.
    match = re.search(r'\((?:[^,()]+,\s*)?([^,()]+)\)\s*$', title)
    if not match:
        match = re.search(r'[–-]\s*([^–-]+)\s*$', title)
    if match:
        city = clean_text(match.group(1))
        if city and len(city.split()) <= 4:
            return venue, city
    return venue, ''


def parse_occurrences(value):
    records = []
    for match in DATE_RE.finditer(value):
        month = MONTHS.get(match.group(2).lower())
        if not month:
            continue
        try:
            event_date = date(
                int(match.group(3)), month, int(match.group(1))
            ).isoformat()
        except ValueError:
            continue
        event_time = None
        if match.group(4):
            event_time = f'{int(match.group(4)):02d}:{match.group(5)}'
        records.append((event_date, event_time))
    return records


def description_from_page(soup):
    parts = []
    header = soup.select_one('.accroche-concert')
    if header:
        for paragraph in header.find_all('p', recursive=False):
            if 'lieu-concert' not in (paragraph.get('class') or []):
                text = clean_text(paragraph)
                if text and text not in parts:
                    parts.append(text)
    body = clean_text(soup.select_one('.concert-droit'))
    if body and body not in parts:
        parts.append(body)
    return '\n\n'.join(parts) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.entry-title'))
    location = soup.select_one('.lieu-concert')
    if not title or not location:
        return []

    location_text = clean_text(location)
    date_match = DATE_RE.search(location_text)
    venue_text = location_text[:date_match.start()].strip() if date_match else ''
    venue, city = parse_location(venue_text, title)
    if not venue or not city:
        return []

    description = description_from_page(soup)
    return [{
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
    } for event_date, time_from in parse_occurrences(location_text)]


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class OrchestreAvignonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestre_avignon_com',
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
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        urls = sitemap_urls(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    event_records = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Orchestre Avignon concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if event_records:
                    records.extend(event_records)
                else:
                    log_message(
                        'Skipped incomplete Orchestre Avignon event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, or city is missing',
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    OrchestreAvignonComCrawler().run()


if __name__ == '__main__':
    main()
