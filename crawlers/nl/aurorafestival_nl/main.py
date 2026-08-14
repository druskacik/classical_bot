import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.aurorafestival.nl/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programma/')
SOURCE = 'Aurora Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = value.get_text(separator, strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def programme_year(session):
    soup = fetch_soup(session, SOURCE_URL)
    match = re.search(r'Aurora Festival\s+(20\d{2})', clean_text(soup))
    if not match:
        raise ValueError('Could not determine Aurora Festival programme year')
    return int(match.group(1))


def parse_location(value):
    venue, separator, city = value.rpartition(',')
    if not separator:
        return None, None
    venue = clean_text(venue)
    city = clean_text(city)
    # This listing reverses the pop-up theatre label after the town name.
    if city.lower().endswith(' - pop up theater'):
        city = re.sub(r'\s+-\s+Pop Up Theater$', '', city, flags=re.I)
        venue = f'Pop Up Theater {venue}'
    return (venue or None), (city or None)


def listing_records(session):
    year = programme_year(session)
    soup = fetch_soup(session, PROGRAMME_URL)
    records = []
    for item in soup.select('ul.performance-list.overview > li.list-item'):
        title = clean_text(item.select_one('.artist-title'))
        location = clean_text(item.select_one('.performance-title'))
        venue, city = parse_location(location)
        day_text = clean_text(item.select_one('.date .date'))
        month_text = clean_text(item.select_one('.date .month')).lower().rstrip('.')
        link = item.select_one('a[href*="/programma/"]')
        url = urljoin(SOURCE_URL, link.get('href', '').strip()) if link else ''
        try:
            event_date = date(year, MONTHS[month_text], int(day_text)).isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', clean_text(item.select_one('.performancetime')))
        if not title or not url or not venue or not city:
            continue
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
            'country_code': 'NL',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(session, url):
    soup = fetch_soup(session, url)
    content = soup.select_one('.concert-page .col-md-8.col-12')
    if not content:
        return None
    parts = []
    for paragraph in content.find_all(['p', 'ul', 'ol'], recursive=False):
        if 'start-date' in paragraph.get('class', []) or paragraph.find_parent(class_='ticket-pod'):
            continue
        text = clean_text(paragraph, separator='\n')
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = listing_records(session)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, record['url']): record
            for record in records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Aurora Festival event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class AuroraFestivalNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aurorafestival_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AuroraFestivalNlCrawler().run()


if __name__ == '__main__':
    main()
