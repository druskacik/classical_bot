import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festival-vezere.com/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Festival de la Vézère'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

# The festival uses venue text rather than a separate city field. These are
# the places represented by its first-party programme filter and its archive.
LOCATION_PATTERNS = [
    (
        r'brive|labenche|rollinat|trois provinces|silab|stadium cab|coll[eè]ge jean moulin',
        'Brive-la-Gaillarde',
    ),
    (r'uzerche|sophie[- ]?dessus|halle huguenot', 'Uzerche'),
    (r'allassac|ardoisi[eè]res', 'Allassac'),
    (r'aubazine|jardins de l.?abbaye', 'Aubazine'),
    (r'clergoux|s[ée]di[eè]res', 'Clergoux'),
    (r'varetz|jardins de colette', 'Varetz'),
    (r'saillant', 'Voutezac'),
    (r'turenne', 'Turenne'),
    (r'objat', 'Objat'),
    (r'travassac|donzenac', 'Donzenac'),
    (r'tulle|cit[ée] de l.accord[ée]on', 'Tulle'),
    (r'saint[- ]ybard|st[- ]ybard|trois saints', 'Saint-Ybard'),
    (r'malemort', 'Malemort'),
    (r'collonges', 'Collonges-la-Rouge'),
    (r'vigeois', 'Vigeois'),
    (r'auriac|jardins sothys', 'Auriac'),
    (r'comborn', 'Orgnac-sur-Vézère'),
]


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%d/%m/%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_location_and_time(value):
    text = clean_text(value)
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3])h([0-5]\d)?', text, re.IGNORECASE)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'

    venue = re.sub(
        r'(?i)\b(?:d[eè]s\s+)?(?:[01]?\d|2[0-3])h(?:[0-5]\d)?'
        r'(?:\s*(?:ou|&|et)\s*(?:[01]?\d|2[0-3])h(?:[0-5]\d)?)?',
        '',
        text,
    )
    venue = re.sub(r'\s*-\s*', ' ', venue)
    venue = re.sub(r'\s+', ' ', venue).strip(' ,;-')
    if not venue:
        return None

    normalized = venue.lower()
    for pattern, city in LOCATION_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            if venue.casefold() == city.casefold():
                return None
            return venue, city, time_from
    return None


def parse_detail(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.node--type-fiche-concert')
    if article is None:
        return None

    title = clean_text(article.select_one('h1'))
    event_date = parse_date(clean_text(article.select_one('.field--name-field-date')))
    location = parse_location_and_time(article.select_one('p.infos'))
    if not title or not event_date or not location:
        return None

    venue, city, time_from = location
    description = clean_text(article.select_one('.field--name-body')) or None
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


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


class FestivalVezereComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festival_vezere_com',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            sitemap = get_response(session, SITEMAP_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Festival de la Vézère sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        sitemap_soup = BeautifulSoup(sitemap.content, 'xml')
        urls = sorted({
            clean_text(location)
            for location in sitemap_soup.find_all('loc')
            if '/concerts/' in clean_text(location)
        })
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(get_response, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_detail(url, future.result().text)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Festival de la Vézère event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    FestivalVezereComCrawler().run()


if __name__ == '__main__':
    main()
