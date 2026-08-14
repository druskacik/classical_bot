import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bbc.co.uk/proms/events/by/date'
SOURCE = 'BBC Proms'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
VENUE_CITIES = {
    'Bristol Beacon': 'Bristol',
    'Royal Albert Hall': 'London',
    'Royal College of Music': 'London',
    "St George's Bristol": 'Bristol',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser'), response.url


def season_month_urls(session):
    soup, final_url = get_soup(session, SOURCE_URL)
    match = re.search(r'/by/date/(\d{4})/', urlparse(final_url).path)
    if not match:
        raise ValueError(f'BBC Proms calendar did not redirect to a dated season: {final_url}')
    year = match.group(1)
    pattern = re.compile(rf'^/proms/events/by/date/{year}/\d{{2}}$')
    urls = {
        urljoin(SOURCE_URL, link['href']).rstrip('/')
        for link in soup.select('a[href]')
        if pattern.match(link.get('href', ''))
    }
    urls.add(final_url.rstrip('/'))
    return sorted(urls)


def detail_venue(session, url):
    """Resolve calendars that show a city where the event card shows a venue."""
    soup, _ = get_soup(session, url)
    info = soup.select_one('.ev-act-summary__performance-information')
    if not info:
        return None
    lines = [line for line in clean_text(info).splitlines() if line]
    for line in reversed(lines):
        if not re.fullmatch(r'\d{1,2}:\d{2}', line) and not re.search(r'\b\d{4}\b', line):
            return line
    return None


def location_fields(session, location, url):
    if ',' in location:
        venue, city = [part.strip() for part in location.rsplit(',', 1)]
        return venue, city
    city = VENUE_CITIES.get(location)
    if city:
        return location, city

    # Some regional Proms cards use only the city as their location label.
    venue = detail_venue(session, url)
    if venue and venue.casefold() != location.casefold():
        return venue, location
    return None, None


def parse_month(session, url):
    soup, _ = get_soup(session, url)
    records = []
    for date_section in soup.select('.ev-event-calendar__single-date-events'):
        date_element = date_section.select_one('.ev-event-calendar__date')
        try:
            date = datetime.strptime(clean_text(date_element), '%a %d %b %Y').date().isoformat()
        except (TypeError, ValueError):
            continue

        for card in date_section.select('.ev-event-calendar__event-summary'):
            title_link = card.select_one('.ev-event-calendar__name a[href]')
            time_element = card.select_one('.ev-event-calendar__time')
            location_element = card.select_one('.ev-event-calendar__event-location')
            title = clean_text(title_link)
            location = clean_text(location_element)
            if not title_link or not title or not location:
                continue
            event_url = urljoin(SOURCE_URL, title_link['href'])
            try:
                venue, city = location_fields(session, location, event_url)
            except requests.RequestException as error:
                log_message(
                    'Failed to resolve BBC Proms event venue',
                    event='crawler_item_failed',
                    level='warning',
                    url=event_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if not venue or not city:
                continue

            description_node = card.select_one('.ev-event-calendar__information')
            description = clean_text(description_node) or None
            time_from = clean_text(time_element) or None
            if time_from and not re.fullmatch(r'\d{2}:\d{2}', time_from):
                time_from = None
            records.append({
                'title': title,
                'date': date,
                'url': event_url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in season_month_urls(session):
        try:
            records.extend(parse_month(session, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape BBC Proms calendar month',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class BbcCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bbc_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BbcCoUkCrawler().run()


if __name__ == '__main__':
    main()
