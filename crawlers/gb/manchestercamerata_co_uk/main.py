import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://manchestercamerata.co.uk/'
SOURCE = 'Manchester Camerata'
ARCHIVE_URLS = (
    f'{SOURCE_URL}performances/',
    f'{SOURCE_URL}series/past-events/',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            'January February March April May June July August September '
            'October November December'
        ).split(),
        start=1,
    )
}
MONTHS.update({name[:3].lower(): number for name, number in list(MONTHS.items())})
DATE_RE = re.compile(
    r'(?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun)(?:day)?\s+)?'
    r'(\d{1,2})(?:\s*[-–]\s*'
    r'(?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun)(?:day)?\s+)?(\d{1,2}))?\s+'
    r'(' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')(?:\s+(20\d{2}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)

VENUE_CITIES = {
    'ao arena': 'Manchester',
    'aviva studios': 'Manchester',
    'rncm': 'Manchester',
    'royal northern college of music': 'Manchester',
    'stoller hall': 'Manchester',
    'the monastery': 'Manchester',
    'manchester monastery': 'Manchester',
    'victoria baths': 'Manchester',
    'wigmore hall': 'London',
    "king's place": 'London',
    'kings place': 'London',
    'blackburn museum': 'Blackburn',
    'national football museum': 'Manchester',
    'the lowry': 'Salford',
    'factory international': 'Manchester',
    'albert hall': 'Manchester',
    'gorton monastery': 'Manchester',
    'new century hall': 'Manchester',
    'bridgewater hall': 'Manchester',
    'manchester cathedral': 'Manchester',
    'home': 'Manchester',
    'royal welsh college': 'Cardiff',
    'darlington hippodrome': 'Darlington',
    'northern school of contemporary dance': 'Leeds',
    'selby abbey': 'Selby',
    'royal albert hall': 'London',
    'isle of wight festival': 'Newport',
    'teatro comunale': 'Ferrara',
    'teatro filarmonico': 'Verona',
    'dubai opera': 'Dubai',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def archive_pages(session, root_url):
    first = BeautifulSoup(get_response(session, root_url).content, 'html.parser')
    page_numbers = [
        int(text)
        for node in first.select('a.page-numbers')
        if (text := clean_text(node)).isdigit()
    ]
    last_page = max(page_numbers, default=1)
    yield first
    for page in range(2, last_page + 1):
        yield BeautifulSoup(
            get_response(session, f'{root_url}page/{page}/').content,
            'html.parser',
        )


def event_urls(session):
    urls = []
    for root_url in ARCHIVE_URLS:
        for soup in archive_pages(session, root_url):
            for card in soup.select('.post-thumb.performances'):
                link = card.select_one('a[href]')
                if not link:
                    continue
                url = link.get('href', '').split('#', 1)[0]
                # Singular /performance/ pages are season, project, or festival
                # overviews. Concrete occurrences use /performances/.
                if urlparse(url).path.startswith('/performances/') and url != root_url:
                    urls.append(url)
    return list(dict.fromkeys(urls))


def parse_dates(text, fallback_year=None):
    match = DATE_RE.search(text)
    if not match:
        return []
    first_day, second_day, month_name, year = match.groups()
    year = year or fallback_year
    if not year:
        return []
    dates = []
    for day in (first_day, second_day):
        if not day:
            continue
        try:
            dates.append(
                datetime(int(year), MONTHS[month_name.lower()], int(day)).date().isoformat()
            )
        except ValueError:
            continue
    return list(dict.fromkeys(dates))


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour)
    minute = int(minute or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    # Several old pages use midnight as an obvious unknown-time placeholder.
    if hour == 12 and minute == 0 and meridiem.lower() == 'am':
        return None
    if meridiem.lower() == 'pm' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def page_year(soup):
    published = soup.select_one('meta[property="article:published_time"]')
    if published:
        match = re.search(r'20\d{2}', published.get('content', ''))
        if match:
            return match.group(0)
    for script in soup.select('script[type="application/ld+json"]'):
        match = re.search(r'"datePublished"\s*:\s*"(20\d{2})-', script.string or '')
        if match:
            return match.group(1)
    return None


def split_location(date_venue):
    without_date = DATE_RE.sub('', date_venue, count=1).strip(' ,–-')
    without_time = TIME_RE.sub('', without_date).replace('&', '').strip(' ,–-')
    parts = [part.strip() for part in without_time.split(',') if part.strip()]
    if not parts:
        return None, None, None

    country_code = 'GB'
    country_names = {
        'ireland': 'IE', 'germany': 'DE', 'france': 'FR', 'spain': 'ES',
        'italy': 'IT', 'austria': 'AT', 'netherlands': 'NL', 'belgium': 'BE',
        'switzerland': 'CH', 'united states': 'US', 'usa': 'US',
    }
    if parts[-1].lower() in country_names:
        country_code = country_names[parts.pop().lower()]

    venue = parts[0]
    city = parts[-1] if len(parts) > 1 else None
    if city and re.search(r'\b(?:arena|hall|museum|monastery|studios?|baths?|college)\b', city, re.I):
        city = None
    if not city:
        lowered = venue.lower()
        city = next((value for key, value in VENUE_CITIES.items() if key in lowered), None)
    if not city:
        parenthetical = re.search(r'\(([^()]+)\)\s*$', venue)
        if parenthetical:
            city = parenthetical.group(1).strip()
    if not city:
        title_city = re.search(r'\bat\s+([A-Z][A-Za-z .’\'-]+)$', venue)
        city = title_city.group(1).strip() if title_city else None
    city_countries = {'Ferrara': 'IT', 'Verona': 'IT', 'Dubai': 'AE'}
    if city in city_countries:
        country_code = city_countries[city]
    return venue, city, country_code


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    header = soup.select_one('main .post-header')
    date_node = header.select_one('.date-venue') if header else None
    title_node = header.select_one('h2') if header else None
    title = clean_text(title_node)
    date_venue = clean_text(date_node).replace('\n', ', ')
    if not title or not date_venue:
        return []

    dates = parse_dates(date_venue, page_year(soup))
    venue, city, country_code = split_location(date_venue)
    if not dates or not venue or not city:
        log_message(
            'Skipping Manchester Camerata page with incomplete event fields',
            event='crawler_item_skipped',
            level='warning',
            url=url,
        )
        return []

    content_node = soup.select_one('main .block-content')
    description = clean_text(content_node) or None
    time_from = parse_time(date_venue)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(future.result().content, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Manchester Camerata event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class ManchesterCamerataCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='manchestercamerata_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ManchesterCamerataCrawler().run()


if __name__ == '__main__':
    main()
