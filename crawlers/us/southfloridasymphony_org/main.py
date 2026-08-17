import json
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://southfloridasymphony.org/'
SOURCE = 'South Florida Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event_listing'
SITEMAP_URL = f'{SOURCE_URL}page-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {}
for number, (short_name, long_name) in enumerate((
    ('Jan', 'January'), ('Feb', 'February'), ('Mar', 'March'),
    ('Apr', 'April'), ('May', 'May'), ('Jun', 'June'),
    ('Jul', 'July'), ('Aug', 'August'), ('Sep', 'September'),
    ('Oct', 'October'), ('Nov', 'November'), ('Dec', 'December'),
), start=1):
    MONTHS[short_name.upper()] = number
    MONTHS[long_name.upper()] = number
WEEKDAYS = {
    'MON', 'MONDAY', 'TUE', 'TUES', 'TUESDAY', 'WED', 'WEDNESDAY',
    'THU', 'THUR', 'THURS', 'THURSDAY', 'FRI', 'FRIDAY',
    'SAT', 'SATURDAY', 'SUN', 'SUNDAY',
}
ARCHIVE_PAGE_RE = re.compile(
    r'/(?:masterworks(?:-[^/]+)?|handels-messiah)/?$', re.I
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def city_and_venue(soup, location_name):
    profile = soup.select_one('.wpem-venue-profile')
    venue = clean_text(profile.select_one('.wpem-venue-name')) if profile else ''
    address = clean_text(profile.select_one('.wpem-venue-description')) if profile else ''
    city = ''
    if address:
        match = re.search(r'(?:^|\n)([A-Za-z .\'-]+),\s*FL\s+\d{5}', address)
        if match:
            city = clean_text(match.group(1))

    location_name = clean_text(location_name)
    if not venue:
        venue = location_name
    if not city and ',' in location_name:
        venue_part, city_part = location_name.rsplit(',', 1)
        if clean_text(city_part):
            venue, city = clean_text(venue_part), clean_text(city_part)

    # This exact first-party venue name is shown on the linked concert page;
    # its season page identifies the occurrence as Miami Shores.
    if not city and 'Broad Performing Arts Center at Barry University' in venue:
        city = 'Miami Shores'
    return city, venue


def parse_event_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    event = event_json_ld(soup)
    if not event:
        return None

    try:
        start = datetime.fromisoformat(str(event['startDate']).replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError):
        return None

    location = event.get('Location') or event.get('location') or {}
    location_name = location.get('name', '') if isinstance(location, dict) else location
    city, venue = city_and_venue(soup, location_name)
    title = clean_text(BeautifulSoup(str(event.get('name', '')), 'html.parser'))
    description = clean_text(BeautifulSoup(str(event.get('description', '')), 'html.parser')) or None
    if not title or not city or not venue:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def api_event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'link'},
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        items = response.json()
        urls.extend(clean_text(item.get('link')) for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return urls


def archive_page_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node)
        path = urlparse(url).path
        if (
            urlparse(url).netloc == urlparse(SOURCE_URL).netloc
            and ARCHIVE_PAGE_RE.search(path)
            and not path.rstrip('/').lower().endswith('-old')
        ):
            urls.append(url)
    return urls


def page_title(soup):
    node = soup.select_one('h1.vc_custom_heading.title-primary, h1.vc_custom_heading.main-title')
    if not node:
        node = soup.select_one('h1.entry-title')
    return clean_text(node)


def parse_archive_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = page_title(soup)
    if not title:
        return []

    main = soup.select_one('article') or soup
    description = clean_text(main) or None
    records = []
    for wrapper in main.select('.wpb_wrapper'):
        headings = [clean_text(node) for node in wrapper.select(':scope > h3, :scope > h4')]
        headings = [value for value in headings if value]
        for index in range(len(headings) - 5):
            city, venue, weekday, month, day, year = headings[index:index + 6]
            if weekday.upper() not in WEEKDAYS or month.upper() not in MONTHS:
                continue
            if not re.fullmatch(r'\d{1,2}', day) or not re.fullmatch(r'20\d{2}', year):
                continue
            venue = clean_text(venue.split('\n')[0])
            try:
                event_date = datetime(int(year), MONTHS[month.upper()], int(day)).date()
            except ValueError:
                continue
            if not city or not venue:
                continue
            records.append({
                'title': title,
                'date': event_date.isoformat(),
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
            break
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    api_urls = api_event_urls(session)
    for url in api_urls:
        try:
            record = parse_event_page(session, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    archive_urls = archive_page_urls(session)
    if api_urls:
        # The landing page repeats the same current Messiah occurrences with
        # less precise titles, venues, and no times than the event API.
        archive_urls = [url for url in archive_urls if not url.rstrip('/').endswith('/handels-messiah')]
    for url in archive_urls:
        try:
            records.extend(parse_archive_page(session, url))
        except requests.RequestException as error:
            log_message(
                'Archive concert page request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'], record['city'])
        unique[key] = record

    if not unique:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['venue']))


class SouthFloridaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='southfloridasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SouthFloridaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
