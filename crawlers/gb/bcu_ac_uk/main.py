import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bcu.ac.uk/conservatoire/events-calendar'
SOURCE = 'Royal Birmingham Conservatoire'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
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
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    soup = get_soup(session, SOURCE_URL)
    urls = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in soup.select('.event-listing-item__actions a[href]')
    }
    return sorted(url for url in urls if '/conservatoire/' in url)


def detail_value(soup, heading):
    label = next(
        (node for node in soup.select('.event-details-heading') if clean_text(node).lower() == heading),
        None,
    )
    if not label:
        return ''
    container = label.find_parent('div', class_=re.compile(r'pll'))
    return clean_text(container)


def parse_dates(value):
    results = []
    # Pages use either one date or a compact inclusive range such as
    # "19 Nov 2026 - 21 Nov 2026" for performances on consecutive days.
    matches = list(re.finditer(r'\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b', value))
    for match in matches:
        try:
            parsed = datetime.strptime(match.group(0), '%d %b %Y').date()
        except ValueError:
            continue
        if parsed not in results:
            results.append(parsed)
    if len(results) == 2 and ' - ' in value and (results[1] - results[0]).days in range(1, 8):
        results = [results[0] + timedelta(days=offset) for offset in range((results[1] - results[0]).days + 1)]
    return [value.isoformat() for value in results]


def parse_time(value):
    date_match = re.search(r'\b\d{4}\b', value)
    tail = value[date_match.end():] if date_match else value
    match = re.search(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', tail, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def resolve_location(value):
    lines = [line for line in value.splitlines() if line.lower() != 'location']
    if not lines:
        return None, None
    venue = lines[0].strip(' ,')
    location = ' '.join(lines).lower()
    if 'shrewsbury' in location:
        city = 'Shrewsbury'
    elif any(term in location for term in ('birmingham', 'b1 ', 'b2 ', 'b3 ', 'b4 ', 'b5 ', 'b6 ')):
        city = 'Birmingham'
    else:
        return None, None
    return (venue, city) if venue and venue.lower() != city.lower() else (None, None)


def event_description(soup):
    body = soup.select_one('.left-col > div:not([class]) > .clear.clear-fix.ptl.pbm')
    return clean_text(body) or None


def parse_event(session, url):
    soup = get_soup(session, url)
    title = re.sub(r'\s+', ' ', clean_text(soup.select_one('main h1'))).strip()
    date_and_time = detail_value(soup, 'date and time')
    venue, city = resolve_location(detail_value(soup, 'location'))
    dates = parse_dates(date_and_time)
    if not title or not dates or not venue or not city:
        return []
    common = {
        'title': title,
        'url': url,
        'time_from': parse_time(date_and_time),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': event_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    return [{**common, 'date': event_date} for event_date in dates]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BcuAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bcu_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BcuAcUkCrawler().run()


if __name__ == '__main__':
    main()
