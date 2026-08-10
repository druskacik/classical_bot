import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.volkstheater-rostock.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan/monatsplan/')
SOURCE = 'Volkstheater Rostock'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_offset(year, month, offset):
    value = year * 12 + month - 1 + offset
    return value // 12, value % 12 + 1


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def city_for_venue(venue):
    lowered = venue.casefold()
    if 'lübeck' in lowered:
        return 'Lübeck'
    if 'hamburg' in lowered or 'laeiszhalle' in lowered:
        return 'Hamburg'
    if 'wismar' in lowered:
        return 'Wismar'
    # The calendar's unqualified halls and public spaces are Rostock venues.
    # An entry called only "Mobil" does not identify a defensible venue.
    if lowered == 'mobil':
        return None
    return 'Rostock'


def listing_record(performance):
    start_node = performance.select_one('meta[itemprop="startDate"][content]')
    title_node = performance.select_one('.performance__title [itemprop="name"]')
    link = performance.select_one('a[itemprop="url"][href]')
    venue_node = performance.select_one('.performance__location')
    if not all((start_node, title_node, link, venue_node)):
        return None

    start = (start_node.get('content') or '').strip()
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?', start)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    venue = clean_text(venue_node.get_text(' ', strip=True))
    city = city_for_venue(venue)
    url = urljoin(SOURCE_URL, link.get('href'))
    if not all((title, venue, city, url)):
        return None

    summary_node = performance.select_one('.performance__infoline')
    summary = clean_text(summary_node.get_text(' ', strip=True)) if summary_node else ''
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': match.group(2),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': summary or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, record):
    soup = get_soup(session, record['url'])
    parts = []
    author = soup.select_one('.production__author')
    if author:
        parts.append(clean_text(author.get_text('\n', strip=True)))
    for node in soup.select('.production .multicol__col--2 > .richtext'):
        value = clean_text(node.get_text('\n', strip=True))
        if value:
            parts.append(value)
    summary = record.get('description') or ''
    description = '\n\n'.join(dict.fromkeys(part for part in parts if part))
    if summary and summary not in description:
        description = f'{summary}\n\n{description}' if description else summary
    return description or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    today = date.today()
    records_by_url = {}

    # The site keeps a server-rendered page per month. Probe the previous two
    # months for any retained archive and the next 16 months for the announced
    # season, including a season published unusually early.
    for offset in range(-2, 17):
        year, month = month_offset(today.year, today.month, offset)
        url = f'{SCHEDULE_URL}{year:04d}-{month:02d}/'
        try:
            soup = get_soup(session, url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape schedule month',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        month_token = f'{year:04d}-{month:02d}'
        selector = (
            '.performance[itemtype="http://schema.org/Event"]'
            f'[data-month-token="{month_token}"]'
        )
        for performance in soup.select(selector):
            record = listing_record(performance)
            if record:
                records_by_url[record['url']] = record

    records = list(records_by_url.values())
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, record): record
            for record in records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class VolkstheaterRostockDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='volkstheater_rostock_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    VolkstheaterRostockDeCrawler().run()


if __name__ == '__main__':
    main()
