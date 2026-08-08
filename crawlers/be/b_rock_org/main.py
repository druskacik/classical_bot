import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://b-rock.org/'
SOURCE = 'B’Rock Orchestra'
CALENDAR_URLS = (
    'https://b-rock.org/calendar/',
    'https://b-rock.org/calendar/archive/',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
COUNTRY_CODE_ALIASES = {'UK': 'GB', 'USA': 'US', 'GERMANY': 'DE'}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_location(item):
    location = item.select_one('.calendar-item-location')
    strong = location.select_one('strong') if location else None
    location_heading = clean_text(strong)
    match = re.match(r'^(?P<city>.+),\s*(?P<country>[A-Za-z]{2,7})\b', location_heading)
    if not match:
        return None

    city = match.group('city').strip()
    raw_country = match.group('country').upper()
    country_code = COUNTRY_CODE_ALIASES.get(raw_country, raw_country)
    # One current listing labels Bonn as BE, while the site's other Bonn
    # listings correctly identify the German city as DE.
    if city.casefold() == 'bonn' and country_code == 'BE':
        country_code = 'DE'
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None

    location_copy = BeautifulSoup(str(location), 'html.parser')
    heading_copy = location_copy.select_one('strong')
    if heading_copy:
        heading_copy.decompose()
    venue = clean_text(location_copy)
    venue = re.sub(r'\s+-\s+(?:cancelled|livestream)\s*$', '', venue, flags=re.I)
    if not city or not venue:
        return None
    return venue, city, country_code


def parse_item(item):
    item_text = clean_text(item)
    if re.search(r'\bcancelled\b', item_text, flags=re.I):
        return None

    date_element = item.select_one('.calendar-item-date strong')
    event_date = parse_date(clean_text(date_element))
    location = parse_location(item)
    project = item.select_one('.calendar-item-project')
    title_element = project.select_one('strong') if project else None
    info_link = item.select_one('.calendar-item-button--info a[href]')
    if not event_date or not location or not title_element or not info_link:
        return None

    title_copy = BeautifulSoup(str(title_element), 'html.parser')
    category = title_copy.select_one('.calendar-item-project-category')
    if category:
        category.decompose()
    title = clean_text(title_copy)
    url = info_link.get('href', '').strip()
    if not title or not url:
        return None

    date_block = clean_text(item.select_one('.calendar-item-date'))
    times = re.findall(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', date_block)
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': times[0] if times else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    sections = []
    project_main = clean_text(soup.select_one('.project-main'))
    if project_main:
        sections.append(project_main)
    for content in soup.select('.content-text'):
        text = clean_text(content)
        if text and text not in sections:
            sections.append(text)
    return '\n\n'.join(sections) or None


class BRockOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='b_rock_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        session.headers.update(HEADERS)
        for url in CALENDAR_URLS:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch B’Rock calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            soup = BeautifulSoup(response.text, 'html.parser')
            records.extend(
                record
                for item in soup.select('.calendar--full-width .calendar-item')
                if (record := parse_item(item)) is not None
            )

        descriptions = {}
        project_urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_description, url): url for url in project_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    descriptions[url] = None
                    log_message(
                        'Failed to fetch B’Rock project details',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['city']
            ),
        )


def main():
    BRockOrgCrawler().run()


if __name__ == '__main__':
    main()
