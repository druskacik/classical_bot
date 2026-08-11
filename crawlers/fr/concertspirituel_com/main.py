import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concertspirituel.com/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'Le Concert Spirituel'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

COUNTRIES = {
    'allemagne': 'DE', 'autriche': 'AT', 'belgique': 'BE',
    'espagne': 'ES', 'hongrie': 'HU', 'italie': 'IT',
    'luxembourg': 'LU', 'pays-bas': 'NL', 'pologne': 'PL',
    'royaume-uni': 'GB', 'suisse': 'CH',
}

CITY_COUNTRIES = {
    'Amsterdam': 'NL', 'Berlin': 'DE', 'Bruxelles': 'BE', 'Budapest': 'HU',
    'Cologne': 'DE', 'Dresde': 'DE', 'Eindhoven': 'NL', 'Gand': 'BE',
    'Groningen': 'NL', 'La Haye': 'NL', 'Londres': 'GB',
    'Monte-Carlo': 'MC', 'Sion': 'CH', 'Utrecht': 'NL',
}

# Venues whose city is present in their official name, but not separated from it.
KNOWN_CITY_MARKERS = (
    'Aix-en-Provence', 'Amsterdam', 'Arromanches-les-Bains', 'Besançon',
    'Budapest', 'Compiègne', 'Eindhoven', 'Gand', 'Groningen', 'La Haye',
    'Londres', 'Massy', 'Megève', 'Metz', 'Monte-Carlo', 'Paris', 'Poitiers',
    'Sézanne',
    'Sion', 'Sisteron', 'Soissons', 'Souvigny', 'Toulouse', 'Utrecht',
    'Urrugne', 'Versailles',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    numbers = [int(item) for item in re.findall(r'\d+', value)]
    if len(numbers) < 3:
        return None
    day, month, year = numbers[:3]
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(?<!\d)(\d{1,2})\s*[hH.]\s*(\d{2})?(?!\d)', value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def location_fields(value):
    location = clean_text(value)
    location = re.sub(r'^Lieu\s*:\s*', '', location, flags=re.I).strip(' -|')
    if not location:
        return None

    country_code = 'FR'
    country_match = re.search(r'\(([^()]*)\)\s*$', location)
    if country_match:
        label = country_match.group(1).strip().lower()
        country_code = COUNTRIES.get(label)
        if not country_code:
            return None
        location = location[:country_match.start()].strip(' ,-')

    city = None
    for marker in KNOWN_CITY_MARKERS:
        if re.search(rf'\b{re.escape(marker)}\b', location, re.I):
            city = marker

    if not city:
        at_match = re.search(r'\b(?:à|de)\s+([A-ZÀ-ÖØ-Ý][\wÀ-ÿ\'’-]*(?:[- ][A-ZÀ-ÖØ-Ý][\wÀ-ÿ\'’-]*){0,2})', location)
        if at_match:
            city = at_match.group(1).strip()

    if not city and '|' in location:
        last = location.rsplit('|', 1)[1].strip()
        last = re.split(r'\s+-\s+', last)[-1].strip()
        if len(last.split()) <= 4 and not re.search(r'festival|opéra|théâtre|[eé]glise|salle|zaal', last, re.I):
            city = last

    if not city:
        dash_tail = re.split(r'\s+-\s+', location)[-1].strip()
        if dash_tail != location and len(dash_tail.split()) <= 4:
            city = dash_tail

    if not city:
        return None
    country_code = CITY_COUNTRIES.get(city, country_code)
    return location, city, country_code


def description_from_page(soup):
    sections = []
    for selector in (
        '.section_chapo-cms', '.wrapper-col-left', '.text-rich-text-p-big'
    ):
        for element in soup.select(selector):
            text = clean_text(element)
            if text and text not in sections:
                sections.append(text)
    return '\n\n'.join(sections) or None


def parse_production(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    if not title:
        return []
    description = description_from_page(soup)
    records = []
    for item in soup.select('a.agenda-spectacle-date-wrapper'):
        date_element = item.select_one('.container-date-rubrique')
        location_element = item.select_one('.lieu-wrapper .p-lieu:last-child')
        event_date = parse_date(clean_text(date_element))
        location = location_fields(clean_text(location_element))
        if not event_date or not location:
            continue
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(clean_text(item)),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_production(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_production(response.text, url)


def discover_productions():
    urls = set()
    pagination = (
        ('12dddace_page', 'a.wrapper_spectacle-info-list'),
        ('378bcbf3_page', 'a.agenda-spectacle-date-wrapper-invite'),
    )
    for parameter, selector in pagination:
        page = 1
        seen_pages = set()
        while True:
            response = requests.get(
                AGENDA_URL,
                params={parameter: page},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            page_urls = {
                urljoin(SOURCE_URL, item.get('href'))
                for item in soup.select(selector)
                if item.get('href')
            }
            signature = tuple(sorted(page_urls))
            if not page_urls or signature in seen_pages:
                break
            seen_pages.add(signature)
            urls.update(page_urls)
            next_page = page + 1
            if not soup.select_one(f'a[href*="{parameter}={next_page}"]'):
                break
            page += 1
    return sorted(urls)


class ConcertSpirituelComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concertspirituel_com',
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
        records = []
        urls = discover_productions()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_production, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Concert Spirituel production',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        unique = {
            (
                item['title'], item['date'], item['time_from'],
                item['venue'], item['city']
            ): item
            for item in records
        }
        return sorted(
            unique.values(),
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    ConcertSpirituelComCrawler().run()


if __name__ == '__main__':
    main()
