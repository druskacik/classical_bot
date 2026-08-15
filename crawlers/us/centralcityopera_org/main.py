import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://centralcityopera.org/'
SOURCE = 'Central City Opera'
SEARCH_API = urljoin(SOURCE_URL, 'wp-json/wp/v2/search')
SPECIAL_EVENTS_URL = urljoin(SOURCE_URL, 'special-events/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|'
    r'October|November|December'
)
OCCURRENCE_PATTERN = re.compile(
    rf'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    rf'(?:{MONTH_PATTERN})\s+\d{{1,2}},\s+20\d{{2}},\s*'
    r'\d{1,2}:\d{2}\s*(?:AM|PM)\b',
    re.IGNORECASE,
)
DATE_TIME_PATTERN = re.compile(
    rf'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    rf'({MONTH_PATTERN})\s+(\d{{1,2}}),\s+(20\d{{2}}),\s*'
    r'(\d{1,2}):(\d{2})\s*(AM|PM)',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    else:
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def parse_datetime(value):
    match = DATE_TIME_PATTERN.search(value or '')
    if not match:
        return None
    try:
        parsed = datetime.strptime(
            ' '.join(match.groups()), '%B %d %Y %I %M %p'
        )
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def search_year(session, year):
    results = []
    page = 1
    while True:
        response = get_response(
            session,
            SEARCH_API,
            params={'search': str(year), 'per_page': 100, 'page': page},
        )
        payload = response.json()
        results.extend(payload)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return results


def occurrence_pages(session):
    pages = {}
    empty_years = 0
    found_occurrences = False
    # Occurrence pages first appeared relatively recently. Searching backwards
    # until ten consecutive empty years avoids hard-coding the oldest season.
    for year in range(date.today().year + 2, 1999, -1):
        year_count = 0
        for item in search_year(session, year):
            title = clean_text(item.get('title'))
            url = item.get('url')
            if (
                item.get('subtype') == 'page'
                and url
                and OCCURRENCE_PATTERN.search(title)
            ):
                pages[url] = title
                year_count += 1
        if year_count:
            found_occurrences = True
            empty_years = 0
        elif found_occurrences:
            empty_years += 1
            if empty_years >= 10:
                break
    return pages


def ticket_url_from_page(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    iframe = soup.select_one('iframe[src*="EventInstanceId="]')
    if iframe is None:
        return None, None

    article = soup.select_one('article')
    description = clean_text(article) if article else ''
    return urljoin(url, iframe.get('src')), description or None


def ticket_record(session, ticket_url, page_url, fallback_description=None):
    soup = BeautifulSoup(get_response(session, ticket_url).text, 'html.parser')
    title = clean_text(soup.select_one('.EventName'))
    date_time = parse_datetime(clean_text(soup.select_one('.DateAndTime')))
    venue = clean_text(soup.select_one('.AreaName'))
    address = clean_text(soup.select_one('.VenueAddress'))
    city_match = re.search(
        r',\s*([^,]+?)(?:,\s*|\s+)CO\s+\d{5}(?:-\d{4})?\b', address
    )
    city = city_match.group(1).strip() if city_match else ''
    if not all((title, date_time, venue, city)):
        return None

    event_date, time_from = date_time
    return {
        'title': title,
        'date': event_date,
        'url': page_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': fallback_description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_occurrence(session, url):
    ticket_url, description = ticket_url_from_page(session, url)
    if not ticket_url:
        return None
    return ticket_record(session, ticket_url, url, description)


def special_event_ticket_urls(session):
    soup = BeautifulSoup(get_response(session, SPECIAL_EVENTS_URL).text, 'html.parser')
    return {
        urljoin(SPECIAL_EVENTS_URL, link['href'])
        for link in soup.select('article a[href*="EventInstanceId="]')
    }


def external_special_records(session):
    """Parse dated first-party cards that do not use the CCO ticket system."""
    soup = BeautifulSoup(get_response(session, SPECIAL_EVENTS_URL).text, 'html.parser')
    modified = soup.select_one('meta[property="article:modified_time"]')
    try:
        listing_year = datetime.fromisoformat(modified['content']).year
    except (KeyError, TypeError, ValueError):
        listing_year = date.today().year

    records = []
    for card in soup.select('.et_pb_blurb'):
        title_node = card.select_one('h4')
        link = card.select_one('a[href]')
        text = clean_text(card)
        title = clean_text(title_node)
        match = re.search(
            rf'\b({MONTH_PATTERN})\s+(\d{{1,2}}),\s*'
            r'(\d{1,2}):(\d{2})\s*(am|pm)\b',
            text,
            re.IGNORECASE,
        )
        if not all((title, link, match)) or 'EventInstanceId=' in link['href']:
            continue

        # This is the only non-ticketed performance venue currently published
        # on this page. Its own linked event page identifies it as Denver.
        venue = 'MCA Central Park Green South' if 'MCA Central Park Green South' in text else None
        if venue is None:
            continue
        try:
            parsed = datetime.strptime(
                f'{match.group(1)} {match.group(2)} {listing_year} '
                f'{match.group(3)} {match.group(4)} {match.group(5)}',
                '%B %d %Y %I %M %p',
            )
        except ValueError:
            continue
        paragraphs = [clean_text(node) for node in card.select('.et_pb_blurb_description p')]
        description = '\n'.join(value for value in paragraphs if value) or None
        records.append({
            'title': title,
            'date': parsed.date().isoformat(),
            'url': urljoin(SPECIAL_EVENTS_URL, link['href']),
            'time_from': parsed.strftime('%H:%M'),
            'venue': venue,
            'city': 'Denver',
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class CentralCityOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='centralcityopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        occurrence_urls = occurrence_pages(session)
        ticket_urls = special_event_ticket_urls(session)
        records = external_special_records(session)

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(scrape_occurrence, session, url): url
                for url in occurrence_urls
            }
            futures.update({
                executor.submit(ticket_record, session, url, url): url
                for url in ticket_urls
            })
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Central City Opera event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        unique = {
            (record['title'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CentralCityOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
