import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://yso.yale.edu/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Yale Symphony Orchestra'

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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for location in soup.select('url > loc'):
        url = clean_text(location).replace('http://yso.yale.edu/', SOURCE_URL, 1)
        if re.match(r'^https://yso\.yale\.edu/events/\d{4}-\d{2}-\d{2}-', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_location(node):
    if not node:
        return '', ''
    lines = [clean_text(line) for line in node.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    venue = lines[0] if lines else ''
    city = ''
    for line in reversed(lines[1:]):
        city_match = re.fullmatch(
            r'([A-Za-z][A-Za-z .\'-]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?',
            line,
        )
        if city_match:
            city = clean_text(city_match.group(1))
            break
    return venue, city


def parse_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title = clean_text(soup.select_one('h1.page-title__heading'))
    time_node = soup.select_one('.event-meta__date time[datetime]')
    venue, city = parse_location(soup.select_one('.event-meta__location'))
    description = clean_text(soup.select_one('.event-meta__description')) or None

    try:
        start = datetime.fromisoformat(time_node['datetime'])
    except (KeyError, TypeError, ValueError):
        return None

    if not title or not venue or not city:
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
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in event_urls(session):
        try:
            record = parse_event(session, url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Yale Symphony Orchestra event',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped incomplete Yale Symphony Orchestra event',
                event='crawler_item_skipped',
                level='warning',
                url=url,
            )

    if not records:
        log_message(
            'No Yale Symphony Orchestra events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class YsoYalecollegeYaleEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yso_yalecollege_yale_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
    YsoYalecollegeYaleEduCrawler().run()


if __name__ == '__main__':
    main()
