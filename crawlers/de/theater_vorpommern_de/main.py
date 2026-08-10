import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-vorpommern.de/de'
SCHEDULE_URL = f'{SOURCE_URL}/spielplan/'
SOURCE = 'Theater Vorpommern'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'Jan.': 1,
    'Feb.': 2,
    'März': 3,
    'Apr.': 4,
    'Mai': 5,
    'Juni': 6,
    'Juli': 7,
    'Aug.': 8,
    'Sept.': 9,
    'Okt.': 10,
    'Nov.': 11,
    'Dez.': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(day_text, month_year_text):
    match = re.fullmatch(r'([^ ]+)\s+(\d{2})', month_year_text)
    if not match or match.group(1) not in MONTHS:
        return None
    try:
        return date(
            2000 + int(match.group(2)),
            MONTHS[match.group(1)],
            int(day_text),
        ).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})[.:](\d{2})\b', value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def resolve_city(venue):
    lowered = venue.lower()
    for city in ('Greifswald', 'Stralsund', 'Putbus'):
        if city.lower() in lowered:
            # A bare city is not a defensible venue.
            if venue.casefold() == city.casefold():
                return None
            return city
    if 'kloster chorin' in lowered:
        return 'Chorin'
    return None


def listing_record(item):
    link = item.select_one('.schedulecontent > a[href^="/de/programm/"]')
    title_node = item.select_one('.scheduleheader .title')
    day_node = item.select_one('.scheduletime .day')
    month_node = item.select_one('.scheduletime .week')
    time_node = item.select_one('.scheduletime .time')
    venue_node = item.select_one('.scheduletime .place')
    if not all((link, title_node, day_node, month_node, venue_node)):
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    venue = clean_text(venue_node.get_text(' ', strip=True))
    event_date = parse_date(
        clean_text(day_node.get_text()), clean_text(month_node.get_text(' ', strip=True))
    )
    city = resolve_city(venue)
    url = urljoin(SOURCE_URL, link.get('href'))
    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(time_node.get_text())) if time_node else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    content = soup.select_one('section.program-detail .content')
    if content is None:
        # Current pages do not label the section consistently; the main content
        # column is stable and excludes dates, ticket buttons, and navigation.
        content = soup.select_one('main .content')
    if content is None:
        return None

    parts = []
    lead = content.select_one('.lead')
    if lead:
        value = clean_text(lead.get_text('\n', strip=True))
        if value:
            parts.append(value)
    for block in content.select('.wysiwyg'):
        # Cast/performer blocks are intentionally excluded. Programme and body
        # prose live before the first Besetzung heading.
        heading = block.select_one('h2')
        if heading and clean_text(heading.get_text()).casefold() == 'besetzung':
            break
        value = clean_text(block.get_text('\n', strip=True))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def calendar_urls(session):
    """Discover productions beyond the initially rendered schedule page."""
    today = date.today()
    urls = set()
    # The endpoint found in the browser's network requests exposes each month
    # separately. Eighteen months covers the announced current/next season.
    for offset in range(18):
        month_index = today.month - 1 + offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        soup = get_soup(session, f'{SOURCE_URL}/updateCal?d={month}_{year}')
        for link in soup.select('.event .title a[href^="/de/programm/"]'):
            urls.add(urljoin(SOURCE_URL, link.get('href')))
    return urls


def detail_records(session, url):
    soup = get_soup(session, url)
    title_node = soup.select_one('main h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    description = detail_description_from_soup(soup)
    records = []
    for item in soup.select('.schedule-list .venue-item'):
        time_node = item.select_one('.time')
        venue_node = item.select_one('.place')
        if not time_node or not venue_node:
            continue
        value = clean_text(time_node.get_text(' ', strip=True))
        match = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', value)
        venue = clean_text(venue_node.get_text(' ', strip=True))
        city = resolve_city(venue)
        if not title or not match or not venue or not city:
            continue
        try:
            event_date = date(
                int(match.group(3)), int(match.group(2)), int(match.group(1))
            ).isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(value),
            'venue': venue,
            'city': city,
            'country_code': 'DE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description_from_soup(soup):
    content = soup.select_one('section.program-detail .content')
    if content is None:
        content = soup.select_one('main .content')
    if content is None:
        return None
    parts = []
    lead = content.select_one('.lead')
    if lead:
        value = clean_text(lead.get_text('\n', strip=True))
        if value:
            parts.append(value)
    for block in content.select('.wysiwyg'):
        heading = block.select_one('h2')
        if heading and clean_text(heading.get_text()).casefold() == 'besetzung':
            break
        value = clean_text(block.get_text('\n', strip=True))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, SCHEDULE_URL)
    records = []
    for item in soup.select('#spielplan > li.schedule-list-item'):
        record = listing_record(item)
        if record:
            records.append(record)

    descriptions = {}
    listed_urls = {record['url'] for record in records}
    urls = sorted(listed_urls | calendar_urls(session))
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record['url'])

    # The rendered schedule can stop before the final announced month. For any
    # production found only through updateCal, its detail page provides the
    # authoritative full date, room/venue, and city-bearing location.
    for url in urls:
        if url not in listed_urls:
            try:
                records.extend(detail_records(session, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar-only production',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class TheaterVorpommernDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_vorpommern_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        return get_concerts()


def main():
    TheaterVorpommernDeCrawler().run()


if __name__ == '__main__':
    main()
