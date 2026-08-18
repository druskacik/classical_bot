import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mb1800.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
SOURCE = 'Music Before 1800'

# Squarespace stores these events using the site's configured timezone. Although
# the venues are in New York, this particular site is configured for Berlin;
# using that timezone reproduces the dates and times displayed by the website.
SITE_TIMEZONE = ZoneInfo('Europe/Berlin')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style, figure, .sqs-block-button-container'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_city(location):
    address = clean_text((location or {}).get('addressLine2'))
    if not address:
        return ''
    return address.split(',', 1)[0].strip()


def parse_event(item):
    title = clean_text(item.get('title'))
    venue = clean_text((item.get('location') or {}).get('addressTitle'))
    city = parse_city(item.get('location'))

    try:
        starts_at = datetime.fromtimestamp(
            int(item['startDate']) / 1000,
            tz=SITE_TIMEZONE,
        )
    except (KeyError, TypeError, ValueError, OSError):
        return None

    path = item.get('fullUrl') or f"/concerts/{item.get('urlId', '')}"
    url = requests.compat.urljoin(SOURCE_URL, path)
    if not title or not venue or not city or not url.startswith(CONCERTS_URL + '/'):
        return None

    description = clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    records = []
    page_url = CONCERTS_URL
    params = {'format': 'json'}
    while True:
        response = session.get(
            page_url,
            params=params,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]

        for item in items:
            record = parse_event(item)
            if record:
                records.append(record)

        next_page_url = (payload.get('pagination') or {}).get('nextPageUrl')
        if not next_page_url:
            break
        page_url = requests.compat.urljoin(SOURCE_URL, next_page_url)
        params = None

    if not records:
        log_message(
            'No concerts found in the Squarespace event feed',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class Mb1800OrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mb1800_org',
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
    Mb1800OrgCrawler().run()


if __name__ == '__main__':
    main()
