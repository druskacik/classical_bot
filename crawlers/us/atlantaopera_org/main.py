import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.atlantaopera.org/'
CALENDAR_URL = f'{SOURCE_URL}whats-on/'
SOURCE = 'The Atlanta Opera'
VENUE_CITY_DEFAULTS = {
    # This venue page links to a named parking location rather than publishing
    # a postal address.  The venue itself is on Georgia Tech's Atlanta campus.
    'Ferst Center for the Arts at Georgia Tech': 'Atlanta',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The current calendar installation exposes records from 2022 onwards.  Start
# well before that boundary so older records are picked up if they are restored.
ARCHIVE_START_YEAR = 2010


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=90)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(attempt + 1)
    raise last_error


def calendar_url(year):
    params = {
        'dtsta': f'{year}-01-01',
        'dtend': f'{year}-12-31',
        'view': 'list',
        'filter_performances': '1',
        'filter_streaming': '0',
        'filter_podcasts': '0',
        'filter_k12_performances': '0',
        'filter_k12_workshops': '0',
        'filter_events': '0',
    }
    return f'{CALENDAR_URL}?{urlencode(params)}'


def parse_calendar(year):
    url = calendar_url(year)
    soup = get_soup(url)
    occurrences = []
    for card in soup.select('.dams-list-past-performances-grid-item'):
        title_node = card.select_one('h4')
        date_node = card.select_one('.dams-ao-23k-performances-grid-date')
        link = card.select_one('a[href*="/production/"]')
        type_node = card.select_one('.dams-ao-23k-performances-grid-type')
        if not all((title_node, date_node, link)):
            continue
        if type_node and clean_text(type_node.get_text()).lower() != 'performance':
            continue
        try:
            event_date = datetime.strptime(
                clean_text(date_node.get_text(' ', strip=True)),
                '%A, %B %d, %Y',
            ).date().isoformat()
        except ValueError:
            continue
        title = clean_text(title_node.get_text(' ', strip=True))
        detail_url = (link.get('href') or '').strip()
        if title and detail_url:
            occurrences.append(
                {'title': title, 'date': event_date, 'detail_url': detail_url}
            )
    return occurrences


def description_from_detail(soup):
    container = soup.select_one('.dams-ao-23k-performance-details-main-info')
    if not container:
        return None

    paragraphs = []
    for node in container.select('.dams-ao-composerlibrettistpremier_info, p'):
        value = clean_text(node.get_text('\n', strip=True))
        if value and value not in paragraphs:
            paragraphs.append(value)
    return '\n\n'.join(paragraphs) or None


def performance_times(soup):
    times = {}
    for ticket in soup.select('.dams-ao-performance-tickets-box a'):
        day_node = ticket.select_one('.day')
        time_node = ticket.select_one('.time')
        if not day_node:
            continue
        day_text = clean_text(day_node.get_text(' ', strip=True))
        day_text = re.sub(r'^[A-Za-z]{3},\s*', '', day_text)
        try:
            event_date = datetime.strptime(day_text, '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
        time_from = None
        if time_node:
            try:
                time_from = datetime.strptime(
                    clean_text(time_node.get_text(' ', strip=True)).upper(),
                    '%I:%M %p',
                ).strftime('%H:%M')
            except ValueError:
                pass
        times[event_date] = time_from
    return times


def venue_city(venue_url, venue):
    soup = get_soup(venue_url)
    for link in soup.select('h3 a[href*="google.com/maps"]'):
        address = clean_text(link.get_text(' ', strip=True))
        if re.search(r'\bAtlanta,\s*GA\s+\d{5}\b', address, re.I):
            return 'Atlanta'
        if re.search(r'\bKennesaw,\s*GA\s+\d{5}\b', address, re.I):
            return 'Kennesaw'
        match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$', address)
        if match:
            city = clean_text(match.group(1))
            # One first-party address writes "NE Atlanta" in the city slot.
            if re.fullmatch(r'(?:N|S|E|W|NE|NW|SE|SW)\s+Atlanta', city, re.I):
                return 'Atlanta'
            return city
    return VENUE_CITY_DEFAULTS.get(venue)


def parse_detail(url):
    soup = get_soup(url)
    info = soup.select_one('.dams-ao-23k-performance-details-main-info')
    venue_link = info.select_one('.venue a[href]') if info else None
    venue = clean_text(venue_link.get_text(' ', strip=True)) if venue_link else ''
    venue_url = (venue_link.get('href') or '').strip() if venue_link else ''
    return {
        'venue': venue,
        'venue_url': venue_url,
        'description': description_from_detail(soup),
        'times': performance_times(soup),
    }


def get_concerts():
    final_year = date.today().year + 3
    occurrences = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(parse_calendar, year): year
            for year in range(ARCHIVE_START_YEAR, final_year + 1)
        }
        for future in as_completed(futures):
            year = futures[future]
            try:
                occurrences.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar year',
                    event='crawler_page_failed',
                    level='warning',
                    url=calendar_url(year),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    detail_urls = sorted({item['detail_url'] for item in occurrences})
    details = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(parse_detail, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    venue_urls = sorted(
        {item['venue_url'] for item in details.values() if item['venue_url']}
    )
    cities = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        venue_names = {
            item['venue_url']: item['venue']
            for item in details.values()
            if item['venue_url']
        }
        futures = {
            executor.submit(venue_city, url, venue_names[url]): url
            for url in venue_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                cities[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape venue detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for occurrence in occurrences:
        detail = details.get(occurrence['detail_url'])
        if not detail:
            continue
        city = cities.get(detail['venue_url'])
        if not detail['venue'] or not city:
            continue
        records.append(
            {
                'title': occurrence['title'],
                'date': occurrence['date'],
                'url': occurrence['detail_url'],
                'time_from': detail['times'].get(occurrence['date']),
                'venue': detail['venue'],
                'city': city,
                'country_code': 'US',
                'description': detail['description'],
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class AtlantaOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='atlantaopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AtlantaOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
