import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.haymusic.org/'
SOURCE = 'Hay Music Trust'
COUNTRY_CODE = 'GB'
FEED_URLS = (
    urljoin(SOURCE_URL, 'events?format=json'),
    urljoin(SOURCE_URL, 'festival-events?format=json'),
)
TIMEZONE = ZoneInfo('Europe/London')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}
POSTCODE_RE = re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', re.IGNORECASE)
KNOWN_CITIES = ('Hay-on-Wye', 'Dorstone', 'Talgarth', 'Llanigon')


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def venue_and_city(item):
    # Squarespace's location object is an unused New York placeholder. Hay
    # Music instead publishes the real venue in the event excerpt/body.
    text = clean_text(item.get('excerpt')) or clean_text(item.get('body'))
    location_line = next(
        (line.strip() for line in text.splitlines() if POSTCODE_RE.search(line)),
        '',
    )
    postcode = POSTCODE_RE.search(location_line)
    if not postcode:
        return None, None

    place = location_line[:postcode.start()].strip(' ,.;:-')
    city = next(
        (name for name in KNOWN_CITIES if re.search(rf'\b{re.escape(name)}\b', place, re.IGNORECASE)),
        None,
    )
    if city:
        venue = re.sub(rf',?\s*{re.escape(city)}\s*$', '', place, flags=re.IGNORECASE).strip(' ,.;:-')
    else:
        # This is Hay Music's local venue calendar. When no town is printed,
        # the named venues in its feed are in Hay-on-Wye.
        venue = place
        city = 'Hay-on-Wye'
    return venue or None, city


def make_record(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    try:
        start = datetime.fromtimestamp(int(item['startDate']) / 1000, tz=TIMEZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None
    venue, city = venue_and_city(item)
    if not all((title, path, venue, city)):
        return None

    description = clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HayMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='haymusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for feed_url in FEED_URLS:
            try:
                response = session.get(feed_url, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Hay Music event feed',
                    event='crawler_feed_failed', level='warning', url=feed_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue

            for item in payload.get('upcoming', []):
                # The festival collection includes a multi-day landing page as
                # well as its individual occurrences. Emit only concrete items.
                if re.fullmatch(
                    r'THE HAY MUSIC FESTIVAL \d{4}',
                    clean_text(item.get('title')),
                    re.IGNORECASE,
                ):
                    continue
                record = make_record(item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Hay Music event with incomplete location or date',
                        event='crawler_item_skipped', level='warning',
                        url=urljoin(SOURCE_URL, item.get('fullUrl', '')),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'], row['title'], row['url']),
        )


def main():
    HayMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
