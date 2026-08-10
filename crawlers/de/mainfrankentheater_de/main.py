import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mainfrankentheater.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'programm/spielplan/')
SOURCE = 'Mainfranken Theater Würzburg'
DEFAULT_CITY = 'Würzburg'

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


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def schedule_months(soup):
    months = {
        node.get('data-filter-token')
        for node in soup.select('.calendar__headermonth[data-filter-token]')
    }
    return sorted(month for month in months if re.fullmatch(r'\d{4}-\d{2}', month or ''))


def listing_entries(soup):
    entries = []
    for node in soup.select('.performance[itemtype="http://schema.org/Event"]'):
        start = node.select_one('[itemprop="startDate"][content]')
        title_node = node.select_one('.performance__headline a')
        venue_node = node.select_one('.performance__location')
        if not start or not title_node or not venue_node:
            continue

        start_value = start.get('content', '')
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):\d{2}', start_value)
        if not match:
            continue
        try:
            event_date = date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            continue

        title = clean_text(title_node)
        venue = clean_text(venue_node)
        url = urljoin(SOURCE_URL, title_node.get('href', ''))
        subtitle = clean_text(node.select_one('.performance__subtitle'))
        if not title or not venue or not url:
            continue

        # The calendar is based in Würzburg. Its explicitly named touring
        # venue is handled separately so the home-city default is not applied.
        city = 'Weikersheim' if 'weikersheim' in venue.lower() else DEFAULT_CITY
        entries.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': f'{match.group(2)}:{match.group(3)}',
            'venue': venue,
            'city': city,
            'subtitle': subtitle,
        })
    return entries


def detail_description(session, entry):
    try:
        soup = get_soup(session, entry['url'])
    except requests.RequestException as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=entry['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return entry['subtitle'] or None

    if soup.select_one('.errorpage') or 'Seite nicht gefunden' in clean_text(soup.title):
        return entry['subtitle'] or None

    parts = []
    for node in soup.select('main.page-content .richtext'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    description = '\n\n'.join(parts)
    if entry['subtitle'] and entry['subtitle'] not in description:
        description = '\n\n'.join(filter(None, (entry['subtitle'], description)))
    return description or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    initial = get_soup(session, SCHEDULE_URL)
    months = schedule_months(initial)

    entries = listing_entries(initial)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, urljoin(SCHEDULE_URL, f'{month}/')): month
            for month in months
        }
        for future in as_completed(futures):
            month = futures[future]
            try:
                entries.extend(listing_entries(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape schedule month',
                    event='crawler_page_failed',
                    level='warning',
                    url=urljoin(SCHEDULE_URL, f'{month}/'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {}
    for entry in entries:
        key = (entry['title'], entry['date'], entry['time_from'], entry['venue'])
        unique[key] = entry

    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, entry): entry
            for entry in unique.values()
        }
        for future in as_completed(futures):
            entry = futures[future]
            record = {key: value for key, value in entry.items() if key != 'subtitle'}
            record.update({
                'country_code': 'DE',
                'description': future.result(),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
            records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class MainfrankentheaterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mainfrankentheater_de',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MainfrankentheaterDeCrawler().run()


if __name__ == '__main__':
    main()
