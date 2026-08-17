import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://stocktonsymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-event-1.xml'
SOURCE = 'Stockton Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

START_DATE_RE = re.compile(r'"startDate"\s*:\s*"([^"]+)"')
VENUE_RE = re.compile(
    r'"location"\s*:\s*\{.*?"@type"\s*:\s*"Place".*?'
    r'"name"\s*:\s*"([^"]+)".*?'
    r'"addressLocality"\s*:\s*"([^"]+)"',
    re.DOTALL,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return [
        clean_text(node.get_text())
        for node in soup.find_all('loc')
        if '/event/' in node.get_text()
    ]


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.find('h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    content = soup.select_one('#page')
    description = clean_text(content.get_text('\n', strip=True) if content else '') or None

    # The site's JSON-LD contains unescaped HTML in its image value, so the
    # complete object is not valid JSON. These fields themselves are stable
    # and safely recoverable from each Event object in the markup.
    starts = START_DATE_RE.findall(html)
    locations = VENUE_RE.findall(html)
    if not title or not starts:
        log_message(
            'Event page is missing required structured fields',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            error_type='MissingEventData',
        )
        return []

    records = []
    for index, value in enumerate(starts):
        try:
            start = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            continue

        location = locations[index] if index < len(locations) else (locations[0] if locations else None)
        if not location:
            continue
        venue, city = map(clean_text, location)
        if not venue or not city:
            continue

        records.append({
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
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    urls = event_urls(response.text)

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_event_page(response.text, url))
        except requests.RequestException as error:
            log_message(
                'Unable to fetch event page',
                event='crawler_event_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class StocktonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stocktonsymphony_org',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    StocktonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
