from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatrwielki.pl/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalendarium/')
SOURCE = 'Teatr Wielki - Opera Narodowa'
# 247 is the site's unlabelled-in-filter-menu "Spektakl muzyczny" taxonomy.
ELIGIBLE_CATEGORY_IDS = {'6', '7', '8', '9', '10', '11', '12', '247'}
WARSAW_VENUES = {
    'sala moniuszki', 'sala młynarskiego', 'sale redutowe', 'foyer główne',
    'iii balkon',
    'teatr wielki - opera narodowa', 'teatr wielki – opera narodowa',
}
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit(('https', 'teatrwielki.pl', parts.path, '', ''))


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    occurrence_match = re.search(r'/termin/(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})/', url)
    expected_date = occurrence_match.group(1) if occurrence_match else ''
    occurrences = soup.select('time[datetime]')
    occurrence = next(
        (item for item in occurrences if item.get('datetime', '')[:10] == expected_date),
        None,
    )
    if occurrence_match and occurrence:
        expected_time = f'{occurrence_match.group(2)}:{occurrence_match.group(3)}'
        exact_occurrence = next(
            (
                item for item in occurrences
                if item.get('datetime', '')[:10] == expected_date
                and expected_time in clean_text(item)
            ),
            None,
        )
        occurrence = exact_occurrence or occurrence
    raw_date = occurrence.get('datetime', '') if occurrence else ''
    try:
        event_date = date.fromisoformat(raw_date[:10]).isoformat()
    except ValueError:
        event_date = ''

    occurrence_text = clean_text(occurrence)
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', occurrence_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    lines = [line.strip() for line in occurrence_text.splitlines() if line.strip()]
    venue = lines[-1] if lines else ''
    if re.search(r'\d{1,2}[:.]\d{2}', venue):
        venue = ''

    city = ''
    city_match = re.search(r'(?:,|\u2013|-)\s*([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż .-]+)$', venue)
    if city_match:
        city = city_match.group(1).strip()
        venue = venue[:city_match.start()].strip(' ,-–')
    elif venue.casefold() in WARSAW_VENUES or venue.casefold().startswith('teatr wielki'):
        city = 'Warszawa'

    description = clean_text(soup.select_one('div.text')) or None
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(url),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': description,
    }


class TeatrWielkiPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrwielki_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def _get(self, session, url):
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return response.text

    def _event_urls(self, session):
        root = BeautifulSoup(self._get(session, CALENDAR_URL), 'html.parser')
        month_urls = {
            canonical_url(urljoin(CALENDAR_URL, link['href']))
            for link in root.select('a[href*="/kalendarium/data/"]')
        }
        event_urls = set()
        for month_url in sorted(month_urls):
            soup = BeautifulSoup(self._get(session, month_url), 'html.parser')
            for item in soup.select('li.data-event'):
                if 'data-multiple' in item.get('class', []):
                    continue
                category_ids = {
                    match.group(1)
                    for cls in item.get('class', [])
                    if (match := re.fullmatch(r'data-category-(\d+)', cls))
                }
                if not category_ids.intersection(ELIGIBLE_CATEGORY_IDS):
                    continue
                link = item.select_one('a[href*="/termin/"]')
                if link:
                    event_urls.add(canonical_url(urljoin(month_url, link['href'])))
        return event_urls

    def scrape(self):
        session = requests.Session()
        urls = self._event_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(self._get, session, url): url
                for url in urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_event_page(future.result(), url)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipped incomplete Teatr Wielki event',
                            event='crawler_item_skipped', level='warning', url=url,
                            error_type='IncompleteEventData',
                            error_message='Required title, date, venue, or city is missing',
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatr Wielki event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    TeatrWielkiPlCrawler().run()


if __name__ == '__main__':
    main()
