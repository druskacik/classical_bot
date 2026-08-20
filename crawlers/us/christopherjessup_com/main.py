import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.christopherjessup.com/'
SOURCE = 'Christopher Jessup'
EVENT_URLS = (
    urljoin(SOURCE_URL, 'events'),
    urljoin(SOURCE_URL, 'past-events'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

US_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI',
    'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI',
    'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC',
    'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT',
    'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*', value, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).upper() == 'PM' and hour != 12:
        hour += 12
    elif match.group(3).upper() == 'AM' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_us_city(value):
    match = re.fullmatch(r'\s*(.+?),\s*([A-Z]{2})\s*', value)
    if not match or match.group(2) not in US_STATE_CODES:
        return None
    return match.group(1).strip()


def parse_card(card, listing_url):
    content = card.select_one('.schedule-list2_content')
    info = card.select_one('.schedule-list2_linfo')
    location_heading = card.select_one('.schedule-list2_image h5')
    subtitles = info.select('.subtitle') if info else []
    performer = clean_text(info.select_one('h6')) if info else ''
    work = clean_text(info.select_one('h4')) if info else ''
    location_text = clean_text(info.select_one('.w-richtext')) if info else ''

    if not content or len(subtitles) < 2 or not performer or not work:
        return None
    event_date = parse_date(clean_text(subtitles[0]))
    city_label = clean_text(location_heading)
    city = parse_us_city(city_label)
    if not event_date or not city or not location_text:
        return None

    venue = re.sub(r'\s*\([^()]+,\s*[A-Z]{2}\)\s*$', '', location_text).strip()
    if not venue or venue.casefold() == city.casefold():
        return None

    link = content.select_one('a.viewport-absolute[href]')
    href = link.get('href', '').strip() if link else ''
    url = urljoin(listing_url, href) if href and href != '#' else listing_url
    description = '\n'.join((
        f'Performer: {performer}',
        f'Work: {work}',
        f'Venue: {location_text}',
    ))
    return {
        'title': f'{performer} — {work}',
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(subtitles[1])),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ChristopherJessupComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='christopherjessup_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        for listing_url in EVENT_URLS:
            try:
                response = session.get(listing_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Christopher Jessup events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=listing_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.select('.schedule-list2_list .w-dyn-item'):
                record = parse_card(card, listing_url)
                if record:
                    records.append(record)

        unique = {
            (record['title'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ChristopherJessupComCrawler().run()


if __name__ == '__main__':
    main()
