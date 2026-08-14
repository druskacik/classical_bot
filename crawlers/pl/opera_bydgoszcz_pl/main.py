import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera.bydgoszcz.pl/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'repertuar.html')
SOURCE = 'Opera Nova w Bydgoszczy'
VENUE = 'Opera Nova'
CITY = 'Bydgoszcz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'styczen': 1,
    'luty': 2,
    'marzec': 3,
    'kwiecien': 4,
    'maj': 5,
    'czerwiec': 6,
    'lipiec': 7,
    'sierpien': 8,
    'wrzesien': 9,
    'pazdziernik': 10,
    'listopad': 11,
    'grudzien': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized_polish(value):
    return value.lower().translate(
        str.maketrans('ąćęłńóśźż', 'acelnoszz')
    )


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_entries(soup):
    calendar = soup.select_one('#calendar')
    if not calendar:
        return []

    entries = []
    today = date.today()
    previous_month = None
    year = today.year

    # The first-party calendar is a rolling, chronological feed. It includes a
    # separate mobile rendering whose simple event cards avoid the empty cells
    # and row-spans used by the desktop calendar.
    for month_panel in calendar.find_all('div', recursive=False):
        heading = month_panel.select_one('h3')
        month = MONTHS.get(normalized_polish(clean_text(heading))) if heading else None
        if not month:
            continue
        if previous_month is None and month < today.month:
            year += 1
        elif previous_month is not None and month < previous_month:
            year += 1
        previous_month = month

        for card in month_panel.select('.d-lg-none > div.text-center.p-1'):
            link = card.select_one('h5 a[href^="/spektakle/"]')
            day_node = card.select_one('h2')
            type_node = card.select_one('h5 small')
            if not link or not day_node:
                continue
            try:
                event_date = date(year, month, int(clean_text(day_node))).isoformat()
            except (TypeError, ValueError):
                continue
            time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', clean_text(card))
            entries.append(
                {
                    'title': clean_text(link),
                    'event_type': clean_text(type_node),
                    'date': event_date,
                    'time_from': time_match.group(0) if time_match else None,
                    'url': urljoin(SOURCE_URL, link.get('href')),
                }
            )
    return entries


def detail_data(session, url):
    soup = get_soup(session, url)
    description_node = soup.select_one('.p-4.user-content')
    description = clean_text(description_node) or None

    # Detail pages expose the full year, unlike the compact calendar. Retain
    # these dates as a check against year rollover in the rolling feed.
    occurrences = set()
    pattern = re.compile(
        r'Data:\s*(\d{2}-\d{2}-\d{4}).*?Godzina:\s*([0-2]?\d:[0-5]\d)',
        re.IGNORECASE,
    )
    for heading in soup.select('h4.text-muted'):
        match = pattern.search(clean_text(heading))
        if not match:
            continue
        try:
            event_date = datetime.strptime(match.group(1), '%d-%m-%Y').date().isoformat()
        except ValueError:
            continue
        occurrences.add((event_date, match.group(2)))
    return description, occurrences


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    entries = calendar_entries(get_soup(session, SCHEDULE_URL))
    detail_by_url = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_data, session, url): url
            for url in {entry['url'] for entry in entries}
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                detail_by_url[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for entry in entries:
        description, occurrences = detail_by_url.get(entry['url'], (None, set()))
        occurrence = (entry['date'], entry['time_from'])
        if occurrences and occurrence not in occurrences:
            matching = [item for item in occurrences if item[1] == entry['time_from']]
            if len(matching) == 1:
                entry['date'] = matching[0][0]

        records.append(
            {
                'title': entry['title'],
                'date': entry['date'],
                'url': entry['url'],
                'time_from': entry['time_from'],
                'venue': VENUE,
                'city': CITY,
                'country_code': 'PL',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )

    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class OperaBydgoszczPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_bydgoszcz_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaBydgoszczPlCrawler().run()


if __name__ == '__main__':
    main()
