import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://landestheater-coburg.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'besuch/spielplan')
SOURCE = 'Landestheater Coburg'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=8,
        pool_maxsize=8,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value, year):
    try:
        return datetime.strptime(f'{value}{year}', '%d.%m.%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def event_description(entry):
    parts = []
    for selector in ('.data-genre', '.data-artist', '.data-addinfo'):
        value = clean_text(entry.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n'.join(parts) or None


def parse_calendar(soup):
    records = []
    for month_group in soup.select('#spielplan-results .event-month-group'):
        month_heading = clean_text(month_group.select_one('.event-month-header'))
        year_match = re.search(r'\b(20\d{2})\b', month_heading)
        if not year_match:
            continue
        year = year_match.group(1)
        for day_group in month_group.select(':scope > .event-day-group'):
            date = parse_date(clean_text(day_group.select_one('.day-label .date')), year)
            if not date:
                continue
            for entry in day_group.select(':scope > .event-entry'):
                title = clean_text(entry.select_one('.title-span'))
                venue = clean_text(entry.select_one('.data-location.is-mobile'))
                time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(
                    entry.select_one('.data-time')
                ))
                detail_link = entry.select_one('.data-link-title-format a[href]')
                ticket_link = entry.select_one('.data-button a[href]')
                link = detail_link or ticket_link
                if not title or not venue or not link:
                    continue
                records.append({
                    'title': title,
                    'date': date,
                    'url': urljoin(CALENDAR_URL, link['href']),
                    'time_from': time_match.group(0) if time_match else None,
                    'venue': venue,
                    'city': 'Coburg',
                    'country_code': 'DE',
                    'description': event_description(entry),
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
    return records


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    subtitle = clean_text(soup.select_one('.page-stueck .page-intro, .page-stueck .page-header'))
    if subtitle:
        parts.append(subtitle)
    for node in soup.select('.page-stueck .module-richtext'):
        value = clean_text(node)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = make_session()
    records = parse_calendar(get_soup(session, CALENDAR_URL))
    detail_urls = sorted({
        record['url'] for record in records
        if record['url'].startswith(urljoin(SOURCE_URL, 'programm/'))
    })
    descriptions = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Landestheater Coburg event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        if detail:
            record['description'] = '\n\n'.join(
                part for part in (record['description'], detail) if part
            )
    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['venue'], item['title'], item['url']
    ))


class LandestheaterCoburgDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='landestheater_coburg_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LandestheaterCoburgDeCrawler().run()


if __name__ == '__main__':
    main()
