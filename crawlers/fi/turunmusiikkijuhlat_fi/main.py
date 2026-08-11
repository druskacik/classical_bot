import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://turunmusiikkijuhlat.fi/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'ohjelma')
SOURCE = 'Turun musiikkijuhlat'
CITY = 'Turku'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))


def event_urls(soup):
    urls = {
        canonical_url(urljoin(PROGRAMME_URL, link.get('href')))
        for link in soup.select('a[href*="/tapahtuma/"]')
        if link.get('href')
    }
    return sorted(url for url in urls if url.startswith(f'{SOURCE_URL}tapahtuma/'))


def event_heading(soup):
    for heading in soup.select('h4.vc_custom_heading'):
        text = clean_text(heading)
        if re.search(r'\b\d{1,2}\.\d{1,2}\.\d{4}\b', text):
            return heading, text
    return None, ''


def parse_date(value):
    match = re.search(r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b', value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_times(value):
    time_part = value.split('|', 1)[1] if '|' in value else ''
    times = []
    for hour, minute in re.findall(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', time_part):
        time = f'{int(hour):02d}:{minute}'
        if time not in times:
            times.append(time)
    return times or [None]


def parse_event(soup, url):
    title = clean_text(soup.select_one('h1.entry-title'))
    date_heading, date_text = event_heading(soup)
    event_date = parse_date(date_text)
    venue_heading = date_heading.find_next_sibling('h4') if date_heading else None
    venue = clean_text(venue_heading)

    content = soup.select_one('.post-content') or soup.select_one('main')
    description = clean_text(content) or None
    if not all((title, event_date, url, venue)):
        return []

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'FI',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in parse_times(date_text)
    ]


def scrape_events():
    urls = event_urls(get_soup(PROGRAMME_URL))
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Turun musiikkijuhlat event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class TurunMusiikkijuhlatFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='turunmusiikkijuhlat_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_events()


def main():
    TurunMusiikkijuhlatFiCrawler().run()


if __name__ == '__main__':
    main()
