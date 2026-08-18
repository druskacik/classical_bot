import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brandywinebaroque.org/'
SOURCE = 'Brandywine Baroque'
SITEMAP_URL = urljoin(SOURCE_URL, 'pages-sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

OCCURRENCE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'[A-Z][a-z]+\s+\d{1,2},\s+20\d{2}\s+at\s+'
    r'\d{1,2}:\d{2}\s+[ap]m$',
    re.IGNORECASE,
)

VENUES = {
    'the barn at flintwoods': ('The Barn at Flintwoods', 'Wilmington'),
    'the lutheran church of our savior': (
        'The Lutheran Church of Our Savior',
        'Rehoboth Beach',
    ),
    'all saints episcopal church': ('All Saints Episcopal Church', 'Rehoboth Beach'),
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_datetime(value):
    try:
        parsed = datetime.strptime(clean_text(value), '%A, %B %d, %Y at %I:%M %p')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_venue(value):
    value = clean_text(value)
    venue_part, separator, city_part = value.partition(',')
    venue_key = venue_part.strip().lower()
    known = VENUES.get(venue_key)
    if not known:
        return None
    venue, default_city = known
    city = city_part.strip() if separator else default_city
    if city.lower() == 'wilmington':
        # The site consistently markets Flintwoods performances as Wilmington,
        # despite the venue's Greenville postal address.
        city = 'Wilmington'
    elif city.lower() == 'rehoboth beach':
        city = 'Rehoboth Beach'
    else:
        city = default_city
    return venue, city


def parse_event_page(soup, url):
    main = soup.select_one('main')
    title_node = main.select_one('h3') if main else None
    schedule_node = main.select_one('h2') if main else None
    title = clean_text(title_node)
    if not title or not schedule_node:
        return []

    lines = [line for line in clean_text(schedule_node).splitlines() if line]
    description_parts = []
    for node in main.select('p'):
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for index, line in enumerate(lines[:-1]):
        if not OCCURRENCE_RE.fullmatch(line):
            continue
        parsed_datetime = parse_datetime(line)
        location = parse_venue(lines[index + 1])
        if not parsed_datetime or not location:
            continue
        event_date, time_from = parsed_datetime
        venue, city = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class BrandywineBaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brandywinebaroque_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        sitemap_response = session.get(SITEMAP_URL, timeout=45)
        sitemap_response.raise_for_status()
        urls = re.findall(r'<loc>\s*(https?://[^<]+)\s*</loc>', sitemap_response.text)

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                # Ignore removed Wix pages which redirect to the season overview.
                if response.url.rstrip('/') != url.rstrip('/'):
                    continue
                records.extend(parse_event_page(
                    BeautifulSoup(response.text, 'html.parser'), url
                ))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Brandywine Baroque page',
                    event='crawler_url_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['venue'], item['title']
        ))


def main():
    BrandywineBaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
