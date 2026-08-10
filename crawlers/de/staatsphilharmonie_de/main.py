import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.staatsphilharmonie.de/de/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programm')
EVENTS_API = f'{PROGRAM_URL}/01-01-2017/json'
SOURCE = 'Deutsche Staatsphilharmonie Rheinland-Pfalz'
FOREIGN_CITY_COUNTRIES = {
    'milano': 'IT',
    'paris': 'FR',
    'toblach (bz)': 'IT',
    'wien': 'AT',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_response(session, url, accept=None):
    headers = {'Accept': accept} if accept else None
    response = session.get(url, headers=headers, timeout=45)
    response.raise_for_status()
    return response


def listing_events(session):
    # This is the endpoint used by the site's calendar. Although its path has
    # a historical start date, it returns the complete archive still published
    # by the site as well as all announced concerts.
    payload = get_response(session, EVENTS_API, 'application/json').json()
    return payload if isinstance(payload, list) else []


def description_from_page(soup):
    parts = []

    programme_heading = next(
        (heading for heading in soup.select('h4') if clean_text(heading).casefold() == 'programm'),
        None,
    )
    if programme_heading:
        programme = programme_heading.find_next_sibling('ul')
        works = [clean_text(item) for item in programme.select('li')] if programme else []
        works = [work for work in works if work]
        if works:
            parts.append('Programm\n' + '\n'.join(works))

    description = soup.select_one('.event_description')
    if description:
        for toggle in description.select('.toggle'):
            toggle.decompose()
        text = clean_text(description, '\n')
        if text:
            parts.append(text)

    return '\n\n'.join(parts) or None


def make_record(event, html):
    relative_url = event.get('url')
    if not relative_url:
        return None
    url = urljoin(SOURCE_URL, relative_url)
    soup = BeautifulSoup(html, 'html.parser')

    title = clean_text(soup.select_one('.page_header_info h1'))
    subtitle = clean_text(soup.select_one('.event_subtitle'))
    if not title:
        title = subtitle
    elif subtitle and subtitle.casefold() not in title.casefold():
        title = f'{title} – {subtitle}'

    try:
        event_date = date.fromisoformat(event.get('startDate', '')).isoformat()
    except (TypeError, ValueError):
        return None

    event_time = clean_text(soup.select_one('.event_time'))
    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', event_time)
    time_from = time_match.group(0) if time_match else None

    location_node = soup.select_one('.event_location')
    if location_node:
        for tooltip in location_node.select('.tooltip_wrapper'):
            tooltip.decompose()
    location = clean_text(location_node)
    city, separator, venue = location.partition(',')
    city = city.strip()
    venue = venue.strip() if separator else ''

    if not title or title.casefold() == 'test' or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': FOREIGN_CITY_COUNTRIES.get(city.casefold(), 'DE'),
        'description': description_from_page(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_response, session, urljoin(SOURCE_URL, event['url'])): event
            for event in events
            if event.get('url')
        }
        for future in as_completed(futures):
            event = futures[future]
            url = urljoin(SOURCE_URL, event.get('url', ''))
            try:
                record = make_record(event, future.result().text)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class StaatsphilharmonieDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='staatsphilharmonie_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    StaatsphilharmonieDeCrawler().run()


if __name__ == '__main__':
    main()
