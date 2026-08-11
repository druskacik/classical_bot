import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaorchestrenormandierouen.fr/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
SOURCE = 'Opéra Orchestre Normandie Rouen'

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
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
}

# These are the institution's Rouen venues. Touring occurrences always carry
# an explicit city before their venue and are never assigned this default.
ROUEN_VENUES = (
    'théâtre des arts',
    'chapelle corneille',
    'salle saint-saëns',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def resolve_location(value):
    location = clean_text(value)
    lowered = location.lower()
    if not location or lowered == 'partout en normandie':
        return None, None

    if lowered.startswith(ROUEN_VENUES):
        return location, 'Rouen'
    if lowered == 'le volcan - scène nationale du havre':
        return location, 'Le Havre'

    if ',' not in location:
        return None, None
    city, venue = (part.strip() for part in location.split(',', 1))
    if not city or not venue:
        return None, None
    return venue, city


def listing_records(soup):
    records = []
    for month_block in soup.select('li.results-item'):
        heading = month_block.select_one('.results-title h2.short')
        heading_match = re.fullmatch(
            r'([a-zéû]+)\s+(\d{4})', clean_text(heading), flags=re.IGNORECASE
        )
        if not heading_match:
            continue
        month = MONTHS.get(heading_match.group(1).lower())
        year = int(heading_match.group(2))
        if not month:
            continue

        for link in month_block.select('a.event-link[href*="/programmation/"]'):
            title = clean_text(link.select_one('.event-title'))
            day_match = re.search(r'\b(\d{1,2})\b', clean_text(link.select_one('.event-dates')))
            venue, city = resolve_location(link.select_one('.event-location'))
            url = urljoin(SOURCE_URL, link.get('href', ''))
            if not title or not day_match or not url or not venue or not city:
                continue
            try:
                event_date = date(year, month, int(day_match.group(1))).isoformat()
            except ValueError:
                continue

            time_text = clean_text(link.select_one('.event-hour'))
            time_match = re.fullmatch(r'(\d{1,2})h(\d{2})', time_text)
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': (
                    f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
                    if time_match else None
                ),
                'venue': venue,
                'city': city,
                'country_code': 'FR',
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def detail_description(session, url):
    soup = get_soup(session, url)
    # The first unclassified section after the hero contains the synopsis,
    # programme, creative team, performers, and work details. Later sections
    # are booking cards and related activities.
    for section in soup.select('main > section'):
        if 'focus' in (section.get('class') or []):
            continue
        if 'has-light-background-color' in (section.get('class') or []):
            continue
        text = clean_text(section)
        if text:
            return text
    return None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = listing_records(get_soup(session, AGENDA_URL))

    descriptions = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in {record['url'] for record in records}
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
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
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class OperaOrchestreNormandieRouenFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaorchestrenormandierouen_fr',
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
        return get_concerts()


def main():
    OperaOrchestreNormandieRouenFrCrawler().run()


if __name__ == '__main__':
    main()
