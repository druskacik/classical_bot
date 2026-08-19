import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.njsymphony.org/'
SOURCE = 'New Jersey Symphony'
PAST_EVENTS_URL = urljoin(SOURCE_URL, 'past-events')
CATEGORY_URLS = [
    urljoin(SOURCE_URL, 'events/category/classical'),
    urljoin(SOURCE_URL, 'events/category/pops-movies'),
    urljoin(SOURCE_URL, 'events/category/family'),
    urljoin(SOURCE_URL, 'events/category/special-presentations'),
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'Newark': 'New Jersey Performing Arts Center',
    'New Brunswick': 'State Theatre New Jersey',
    'Princeton': 'Richardson Auditorium',
    'Morristown': 'Mayo Performing Arts Center',
    'Red Bank': 'Count Basie Center for the Arts',
    'Jersey City': 'Symphony Center',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_inline(value):
    return re.sub(r'\s+', ' ', clean_text(value)).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items(soup):
    items = {}
    for item in soup.select('.eventItem'):
        link = item.select_one('h3.title a[href*="/events/detail/"]')
        if link:
            items[urljoin(SOURCE_URL, link.get('href'))] = item
    return items


def listing_description(item):
    parts = []
    for selector in ('.tagline', '.event_item_info', '.link[data-options-layout="audio"]'):
        text = clean_text(item.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_single_listing_date(item):
    date_node = item.select_one('.date .m-date__singleDate')
    if not date_node:
        return None
    try:
        value = re.sub(r'\s+,', ',', clean_inline(date_node))
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(value, '%b %d, %Y').date().isoformat()
        except ValueError:
            return None


def listing_location(item):
    location = clean_text(item.select_one('.meta .location'))
    match = re.fullmatch(r'Performed in ([A-Za-z .]+)', location)
    if not match:
        return None, None
    city = match.group(1).strip()
    evidence = clean_inline(item)
    venue = next((name for name in VENUES.values() if name in evidence), None)
    if not venue:
        title = clean_inline(item.select_one('h3.title'))
        title_match = re.search(r'\bat (.+?) in ' + re.escape(city) + r'$', title, re.I)
        if title_match:
            venue = title_match.group(1).strip()
    if not venue:
        held_match = re.search(
            r'\bheld (?:this year )?at (?:the )?(.+?)(?:, located| in ' +
            re.escape(city) + r')', evidence, re.I,
        )
        if held_match:
            venue = held_match.group(1).strip()
    return (venue, city) if venue else (None, None)


def archived_record(url, item):
    title = clean_inline(item.select_one('h3.title'))
    event_date = parse_single_listing_date(item)
    venue, city = listing_location(item)
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': listing_description(item),
    }


def detail_description(soup, fallback=None):
    parts = []
    for selector in ('.event_heading .tagline', '.event_description',
                     '.link[data-options-layout="audio"]'):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or fallback


def detail_records(url, soup, fallback=None):
    title = clean_inline(soup.select_one('.event_heading .title'))
    year_node = soup.select_one('.event_heading .date .m-date__year')
    if not title or not year_node:
        return []
    year_match = re.search(r'\b(20\d{2})\b', clean_text(year_node))
    if not year_match:
        return []
    year = int(year_match.group(1))
    description = detail_description(soup, fallback)
    records = []
    for showing in soup.select('.event_showings li.listItem'):
        date_text = clean_text(showing.select_one('.m-date__singleDate'))
        time_text = clean_text(showing.select_one('.time'))
        venue_text = clean_text(showing.select_one('.showing_venue'))
        location_match = re.fullmatch(r'(.+?) in ([A-Za-z .]+)', venue_text)
        if not date_text or not location_match:
            continue
        try:
            event_date = datetime.strptime(f'{date_text} {year}', '%a, %b %d %Y').date().isoformat()
        except ValueError:
            continue
        try:
            time_from = datetime.strptime(time_text.upper(), '%I:%M %p').strftime('%H:%M')
        except ValueError:
            time_from = None
        venue, city = (part.strip() for part in location_match.groups())
        if venue and city:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
            })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    current_items = {}
    for category_url in CATEGORY_URLS:
        current_items.update(listing_items(get_soup(session, category_url)))

    past_items = listing_items(get_soup(session, PAST_EVENTS_URL))
    records = [record for url, item in past_items.items()
               if (record := archived_record(url, item))]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): (url, item)
                   for url, item in current_items.items()}
        for future in as_completed(futures):
            url, item = futures[future]
            try:
                records.extend(detail_records(url, future.result(), listing_description(item)))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape New Jersey Symphony event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(records, key=lambda row: (
        row['date'], row['time_from'] or '', row['title'], row['venue']))


class NjSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='njsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    NjSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
