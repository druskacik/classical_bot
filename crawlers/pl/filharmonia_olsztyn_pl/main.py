import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonia.olsztyn.pl/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/koncerty'
SOURCE = 'Warmińsko-Mazurska Filharmonia im. F. Nowowiejskiego w Olsztynie'
DEFAULT_CITY = 'Olsztyn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'styczeń': 1, 'stycznia': 1, 'luty': 2, 'lutego': 2,
    'marzec': 3, 'marca': 3, 'kwiecień': 4, 'kwietnia': 4,
    'maj': 5, 'maja': 5, 'czerwiec': 6, 'czerwca': 6,
    'lipiec': 7, 'lipca': 7, 'sierpień': 8, 'sierpnia': 8,
    'wrzesień': 9, 'września': 9, 'październik': 10,
    'października': 10, 'listopad': 11, 'listopada': 11,
    'grudzień': 12, 'grudnia': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(\d{1,2}),?\s+(20\d{2})', value)
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    details = soup.select_one('main .event-details')
    title = clean_text(details.select_one('h1, h2, .title')) if details else ''
    if not title:
        heading = soup.select_one('main h1')
        title = clean_text(heading)
    day = clean_text(soup.select_one('main .event-info .day'))
    event_date = parse_date(day)
    time_text = clean_text(soup.select_one('main .event-info .hour'))
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', time_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    venue = clean_text(soup.select_one('main .event-info .place'))
    description = clean_text(soup.select_one('main .event-content')) or None
    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': DEFAULT_CITY,
        'country_code': 'PL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(item):
    url = item.get('link', '').strip()
    if not url:
        return None
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class FilharmoniaOlsztynPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_olsztyn_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        events = []
        page = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'desc',
                    '_fields': 'id,link',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            events.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_event, item): item for item in events}
            for future in as_completed(futures):
                url = futures[future].get('link', '')
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Filharmonia Olsztyn event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Filharmonia Olsztyn event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or URL is missing',
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    FilharmoniaOlsztynPlCrawler().run()


if __name__ == '__main__':
    main()
