import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.citemusicale-metz.fr/'
SOURCE = 'Cité musicale-Metz'
EVENT_DATES_API = urljoin(SOURCE_URL, 'api/event_dates')

# These are the site's first-party "Styles" values which can contain events
# covered by the project's inclusion guidance. The API treats repeated
# tagGroup[] values as AND, so each style must be paginated separately.
IN_SCOPE_STYLE_SLUGS = (
    'symphonique',
    'musique-baroque',
    'grandes-voix',
    'musique-de-chambre',
    'choeurs',
    'piano',
    'danse',
    'musique-nouvelle',
    'cirque-musical',
    'theatre-musical',
    'musique-vocale',
    'cine-concert',
    'conte-musical',
    'creation-et-crossover',
    'recital',
)

HEADERS = {
    'Accept': 'application/ld+json',
    'Accept-Language': 'fr-FR,fr;q=0.9',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def iter_style_dates(session, style_slug):
    url = EVENT_DATES_API
    params = {
        '_locale': 'fr',
        'tagGroup[]': style_slug,
        'order[startDate]': 'asc',
    }
    while url:
        payload = get_json(session, url, params=params)
        yield from payload.get('hydra:member') or []
        next_url = (payload.get('hydra:view') or {}).get('hydra:next')
        url = urljoin(SOURCE_URL, next_url) if next_url else None
        params = None


def listing_dates(session):
    occurrences = {}
    for style_slug in IN_SCOPE_STYLE_SLUGS:
        try:
            for occurrence in iter_style_dates(session, style_slug):
                occurrence_id = occurrence.get('@id')
                if occurrence_id:
                    occurrences[occurrence_id] = occurrence
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch a programme style feed',
                event='crawler_page_failed',
                level='warning',
                url=EVENT_DATES_API,
                style=style_slug,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return list(occurrences.values())


def event_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return None
    lines = [clean_text(line) for line in main.get_text('\n').splitlines()]
    lines = [line for line in lines if line]

    starts = ('Présentation', 'Programme', 'Distribution')
    start = next((index for index, line in enumerate(lines) if line in starts), None)
    if start is None:
        return None
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index] in ('Ajouter aux favoris', 'Osez !')
        ),
        len(lines),
    )
    return clean_text('\n'.join(lines[start:end])) or None


def fetch_descriptions(session, occurrences):
    urls = {
        urljoin(SOURCE_URL, occurrence.get('event', {}).get('url', ''))
        for occurrence in occurrences
        if occurrence.get('event', {}).get('url')
    }
    descriptions = {}

    def fetch(url):
        response = session.get(url, timeout=60)
        response.raise_for_status()
        return event_description(response.text)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None
    return descriptions


def make_record(occurrence, descriptions):
    event = occurrence.get('event') or {}
    place = occurrence.get('place') or {}
    address = place.get('address') or {}
    title = clean_text(event.get('name'))
    venue = clean_text(place.get('name'))
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper()
    relative_url = event.get('url') or ''
    url = urljoin(SOURCE_URL, relative_url)

    try:
        start = datetime.fromisoformat(occurrence.get('startDate', ''))
        event_date = start.date().isoformat()
    except (TypeError, ValueError):
        return None

    if not all((title, venue, city, country_code, relative_url)):
        return None
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': descriptions.get(url),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    occurrences = listing_dates(session)
    descriptions = fetch_descriptions(session, occurrences)
    records = [make_record(occurrence, descriptions) for occurrence in occurrences]
    records = [record for record in records if record]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'], record['title'], record['venue']
        ),
    )


class CitemusicaleMetzFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='citemusicale_metz_fr',
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
    CitemusicaleMetzFrCrawler().run()


if __name__ == '__main__':
    main()
