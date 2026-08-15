import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://azopera.org/'
SOURCE = 'Arizona Opera'
PERFORMANCES_URL = urljoin(SOURCE_URL, 'performances')
EVENTS_URL = urljoin(SOURCE_URL, 'events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def performance_venue_map(row):
    venues = {}
    for field in row.select('[class*="views-field-field-performance-dates-"]'):
        city_node = field.select_one('.views-label')
        value_node = field.select_one('.field-content')
        city, value = clean_text(city_node), clean_text(value_node)
        if not city or not value:
            continue
        parts = re.split(r'\s+[–—]\s+', value, maxsplit=1)
        if len(parts) == 2 and clean_text(parts[1]):
            venues[city] = clean_text(parts[1])
    return venues


def description_from_page(soup):
    pane = soup.select_one('.performance-description, .pane-node-body, .field-name-body')
    if not pane:
        return None
    for unwanted in pane.select('script, style, .buy-link, .tickets'):
        unwanted.decompose()
    text = pane.get_text('\n', strip=True)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def parse_performance(session, url, venues):
    soup = get_soup(session, url)
    title_node = soup.select_one('h1.title, .performance-title .title')
    title = clean_text(title_node)
    description = description_from_page(soup)
    records = []

    for group in soup.select('.performance-showtimes .showtime-group'):
        city = clean_text(group.select_one('h3'))
        venue = venues.get(city)
        if not title or not city or not venue:
            continue
        for node in group.select('[property="dc:date"][content]'):
            raw = node.get('content', '')
            try:
                instant = datetime.fromisoformat(raw)
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': instant.date().isoformat(),
                'url': url,
                'time_from': instant.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def event_venue(soup):
    # Event pages expose their venue in the first-party map marker link.
    for link in soup.select('a[href*="/attending-opera/maps-and-directions/"]'):
        venue = clean_text(link)
        if venue:
            return venue
    for script in soup.select('.pane-getlocations-map script'):
        match = re.search(
            r'"latlons"\s*:\s*\[\["[^"]*","[^"]*","[^"]*","([^"]+)"',
            script.string or script.get_text(),
        )
        if match and clean_text(match.group(1)):
            return clean_text(match.group(1))
    return ''


def parse_event(session, url, fallback_date=None, fallback_city=''):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('h1.title'))
    venue = event_venue(soup)
    date_node = soup.select_one('[property="dc:date"][content]')
    raw = date_node.get('content', '') if date_node else ''
    try:
        instant = datetime.fromisoformat(raw)
    except ValueError:
        instant = fallback_date
    city = fallback_city
    heading = soup.select_one('.event-date, .pane-event-page-pieces')
    heading_text = clean_text(heading)
    match = re.search(r'\((Phoenix|Tucson)\)', heading_text)
    if match:
        city = match.group(1)
    if not all((title, instant, venue, city)):
        return None
    return {
        'title': title,
        'date': instant.date().isoformat(),
        'url': url,
        'time_from': instant.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_page(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    listing = get_soup(session, PERFORMANCES_URL)
    records = []

    for row in listing.select('.view-performances .performance-row'):
        link = row.select_one('.views-field-title a[href]')
        if not link:
            continue
        url = urljoin(PERFORMANCES_URL, link['href'])
        try:
            records.extend(parse_performance(session, url, performance_venue_map(row)))
        except requests.RequestException as error:
            log_message(
                'Performance page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    # The calendar is a mixed but necessary adjacent feed. Its stable month URLs
    # expose eligible recitals/previews as well as talks, so records go through
    # potential-event classification. Follow the site's Next link until exhausted.
    calendar_url = EVENTS_URL
    seen_months = set()
    for _ in range(24):
        if calendar_url in seen_months:
            break
        seen_months.add(calendar_url)
        calendar = get_soup(session, calendar_url)
        for row in calendar.select('.view-content .views-row'):
            link = row.select_one('.views-field-field-performance a[href^="/events/"]')
            stamp = row.select_one('[property="dc:date"][content]')
            city = clean_text(row.select_one('.views-field-field-location .field-content'))
            if not link or not stamp:
                continue
            try:
                instant = datetime.fromisoformat(stamp.get('content', ''))
            except ValueError:
                continue
            url = urljoin(calendar_url, link['href'])
            try:
                record = parse_event(session, url, instant, city)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        next_link = calendar.select_one('.pager-next a[href]')
        if not next_link:
            break
        calendar_url = urljoin(calendar_url, next_link['href'])

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No valid concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=PERFORMANCES_URL,
            record_count=0,
        )
    return result


class AzoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='azopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    AzoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
