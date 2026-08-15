import concurrent.futures
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://qatarphilharmonicorchestra.org/'
SOURCE = 'Qatar Philharmonic Orchestra'
SITEMAP_URL = f'{SOURCE_URL}tc_events-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date_time(value):
    match = re.search(
        r'\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+'
        r'(\d{1,2}:\d{2}\s*[AP]M)\b',
        value,
    )
    if not match:
        return None, None
    try:
        parsed_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        parsed_time = datetime.strptime(match.group(2).replace(' ', ''), '%I:%M%p').strftime('%H:%M')
    except ValueError:
        return None, None
    return parsed_date, parsed_time


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node)
        path = urlparse(url).path
        if not path.startswith('/concert/') or path == '/concert/':
            continue
        if path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            continue
        urls.append(url)
    return list(dict.fromkeys(urls))


def description_from(entry):
    details = entry.select_one('.tc_the_content_pre')
    if details:
        details.extract()
    for node in entry.select('script, style, form, nav'):
        node.extract()
    description = clean_text(entry)
    return description or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    entry = soup.select_one('.entry-content')
    heading = soup.select_one('h1.entry-title, h1')
    date_node = soup.select_one('.tc_event_date_title_front')
    venue_node = soup.select_one('.tc_event_location_title_front')

    title = clean_text(heading)
    venue = clean_text(venue_node)
    event_date, time_from = parse_date_time(clean_text(date_node))
    if not all((title, event_date, venue, url)):
        return None

    # QPO's first-party calendar is based in Doha. Its listed Qatar venues are
    # in the Doha metropolitan area; no foreign touring dates are assumed.
    city = 'Doha'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'QA',
        'description': description_from(entry) if entry else None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class QatarPhilharmonicOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='qatarphilharmonicorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='QA',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch_event(self, url):
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            return parse_event(response.text, url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Qatar Philharmonic Orchestra concert',
                event='crawler_event_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        try:
            response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Qatar Philharmonic Orchestra sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = event_urls(response.content)
        if not urls:
            raise ValueError('Qatar Philharmonic Orchestra sitemap returned no English concerts')

        # A small pool keeps a full archive run practical without overwhelming
        # this comparatively slow WordPress host.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            records = [record for record in executor.map(self.fetch_event, urls) if record]
        return records


def main():
    return QatarPhilharmonicOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
