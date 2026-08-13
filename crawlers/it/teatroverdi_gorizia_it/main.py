import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroverdi.gorizia.it/'
PROJECTS_API = f'{SOURCE_URL}wp-json/wp/v2/project'
SOURCE = 'Teatro Comunale Giuseppe Verdi di Gorizia'
CITY = 'Gorizia'
THEATRE_VENUE = 'Teatro Comunale Giuseppe Verdi'
SUMMER_VENUE = 'Giardino interno di Palazzo de Grazia'
SUMMER_CATEGORY_ID = 54

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def listing_projects(session):
    projects = []
    page = 1
    while True:
        response = get_response(
            session,
            PROJECTS_API,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,date,link,title,content,pj-categs',
            },
        )
        projects.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return projects


def detail_value(soup, label):
    for item in soup.select('.project_details_item'):
        title = item.select_one('.project_details_item_title')
        value = item.select_one('.project_details_item_desc')
        if title and value and clean_text(title.get_text()).casefold() == label.casefold():
            return clean_text(value.get_text(' ', strip=True))
    return ''


def parse_date(value, fallback_year=None):
    match = re.search(
        r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(\d{4}))?',
        value.casefold(),
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else fallback_year
    if not year:
        return None
    try:
        return date(
            year, MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})[:.]([0-5]\d)\b', value)
    if not match or int(match.group(1)) > 23:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def make_record(session, project):
    url = project.get('link') or ''
    if not url:
        return None
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    # One current-season page omits the year. Its publication timestamp is a
    # stable first-party fallback; all other detail pages publish the full year.
    published = project.get('date') or ''
    published_match = re.match(r'(\d{4})-', published)
    fallback_year = int(published_match.group(1)) if published_match else None
    event_date = parse_date(
        detail_value(soup, 'Data Rappresentazione'), fallback_year=fallback_year
    )
    if not event_date:
        return None

    title = clean_text((project.get('title') or {}).get('rendered'))
    title = re.sub(r'^\d{1,2}\s+[A-Za-zÀ-ÿ]+\s*[–—-]\s*', '', title).strip()
    categories = project.get('pj-categs') or []
    venue = SUMMER_VENUE if SUMMER_CATEGORY_ID in categories else THEATRE_VENUE
    if not title or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(detail_value(soup, 'Inizio Rappresentazione')),
        'venue': venue,
        'city': CITY,
        'country_code': 'IT',
        'description': clean_text((project.get('content') or {}).get('rendered')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    projects = listing_projects(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(make_record, session, project): project
            for project in projects
        }
        for future in as_completed(futures):
            project = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=project.get('link'),
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


class TeatroverdiGoriziaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroverdi_gorizia_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
    TeatroverdiGoriziaItCrawler().run()


if __name__ == '__main__':
    main()
