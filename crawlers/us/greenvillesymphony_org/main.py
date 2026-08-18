import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.greenvillesymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap_index.xml'
SOURCE = 'Greenville Symphony Orchestra'
CITY = 'Greenville'

# The calendar is local to greater Greenville. These satellite venues are the
# exceptions to the organization's otherwise defensible Greenville default.
VENUE_CITIES = {
    'Blue Ridge Library Branch': 'Greer',
    'Bridge City Coffee - Travelers Rest': 'Travelers Rest',
    'Bridgeway Station Farmers Market': 'Mauldin',
    'Five Forks Branch': 'Simpsonville',
    'Greer Branch': 'Greer',
    'Taylors Branch': 'Taylors',
    'The Southern Growl': 'Greer',
    'Travelers Rest Branch': 'Travelers Rest',
}

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
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(sitemap_text):
    soup = BeautifulSoup(sitemap_text, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        if re.fullmatch(r'https://www\.greenvillesymphony\.org/gso_event/[^/]+/', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%H:%M')
    except ValueError:
        return None


def json_ld_events(soup):
    events = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                events.append(candidate)
    return events


def parse_event_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    page_title = clean_text(soup.select_one('h1').get_text(' ', strip=True)) if soup.select_one('h1') else ''
    details = soup.select_one('.event-details')
    description = clean_text(details.get_text('\n', strip=True)) if details else None

    records = []
    for event in json_ld_events(soup):
        title = clean_text(unescape(event.get('name') or page_title))
        start = event.get('startDate')
        location = event.get('location') or {}
        venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
        try:
            event_date = datetime.fromisoformat(str(start).replace('Z', '+00:00')).date().isoformat()
        except (TypeError, ValueError):
            continue
        if not title or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(start),
            'venue': venue,
            'city': VENUE_CITIES.get(venue, CITY),
            'country_code': 'US',
            'description': description,
        })
    return records


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event_page(url, response.text)


def scrape_concerts():
    response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    urls = event_urls(response.text)
    if not urls:
        log_message(
            'No event URLs found in sitemap',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
        return []

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class GreenvilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='greenvillesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    GreenvilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
