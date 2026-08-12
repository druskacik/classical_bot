import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://stefanotempia.it/'
SOURCE = 'Accademia Corale Stefano Tempia'
PROJECTS_API = f'{SOURCE_URL}wp-json/wp/v2/project'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1,
    'febbraio': 2,
    'marzo': 3,
    'aprile': 4,
    'maggio': 5,
    'giugno': 6,
    'luglio': 7,
    'agosto': 8,
    'settembre': 9,
    'ottobre': 10,
    'novembre': 11,
    'dicembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(value):
    address = clean_text(value)
    if not address:
        return None

    city = None
    postal_match = re.search(r'\b\d{5}\s*,?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ’\' -]*?)(?:\s*\([A-Z]{2}\)|$)', address)
    if postal_match:
        city = postal_match.group(1).strip(' ,-')
    if not city:
        for known_city in ('Torino', 'Agliè', 'Pralormo'):
            if re.search(rf'\b{re.escape(known_city)}\b', address, re.IGNORECASE):
                city = known_city
                break

    street_match = re.search(
        r'\s+(?:-|–)?\s*(?=(?:Via|Viale|Piazza|Piazzale|Corso|Largo)\b)',
        address,
        re.IGNORECASE,
    )
    venue = address[:street_match.start()].strip(' ,-') if street_match else address
    if not city or not venue:
        return None
    return venue, city


def project_details(soup):
    details = {}
    for item in soup.select('.project_details_item'):
        key = clean_text(item.select_one('.project_details_item_title')).rstrip(':').lower()
        value = clean_text(item.select_one('.project_details_item_desc'))
        if key and value:
            details[key] = value
    return details


def parse_project(project, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text((project.get('title') or {}).get('rendered'))
    url = project.get('link') or ''
    details = project_details(soup)
    event_date = parse_date(details.get('data', ''))
    location = parse_location(details.get('indirizzo', ''))
    if not title or not url or not event_date or not location:
        return None

    venue, city = location
    description = clean_text(soup.select_one('.cmsmasters_project_content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(details.get('orario', '')),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_projects(session):
    projects = []
    page = 1
    while True:
        response = session.get(
            PROJECTS_API,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,title,pj-categs',
            },
            timeout=45,
        )
        response.raise_for_status()
        projects.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return projects
        page += 1


class StefanoTempiaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stefanotempia_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
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
            projects = fetch_projects(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Stefano Tempia project feed',
                event='crawler_fetch_failed',
                level='error',
                url=PROJECTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, project.get('link'), timeout=45): project
                for project in projects if project.get('link')
            }
            for future in as_completed(futures):
                project = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_project(project, response.text)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Stefano Tempia concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=project.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    StefanoTempiaItCrawler().run()


if __name__ == '__main__':
    main()
