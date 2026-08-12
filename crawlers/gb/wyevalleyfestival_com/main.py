import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wyevalleyfestival.com/'
SOURCE = 'Wye Valley Chamber Music'
EVENT_SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-event-1.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

# The site presents venues as a comma-separated venue and address. These are
# the localities used by its current and archived event catalogue.
LOCALITIES = {
    'chepstow': 'Chepstow',
    'hereford': 'Hereford',
    'ledbury': 'Ledbury',
    'monmouth': 'Monmouth',
    'st briavels': 'St Briavels',
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
    normalized = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', value, flags=re.IGNORECASE)
    try:
        return datetime.strptime(normalized, '%A %d %B, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([ap]m)\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    if not 1 <= hour <= 12:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{match.group(2)}'


def parse_location(value):
    lowered = value.lower()
    city = next((city for key, city in LOCALITIES.items() if key in lowered), None)
    venue = value.split(',', 1)[0].strip()
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city


def labelled_section(soup, label_name):
    label = next(
        (item for item in soup.select('main label') if clean_text(item).casefold() == label_name.casefold()),
        None,
    )
    if label is None:
        return ''

    parts = []
    for sibling in label.next_siblings:
        name = getattr(sibling, 'name', None)
        if name == 'label':
            break
        if name in {'p', 'ul', 'ol'}:
            text = clean_text(sibling)
            if text:
                parts.append(text)
    return '\n\n'.join(parts)


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.post-title'))
    event_date = parse_date(clean_text(soup.select_one('p.date')))
    location = parse_location(clean_text(soup.select_one('p.venue')))
    if not title or not event_date or not location:
        return None

    artists = labelled_section(soup, 'Artists')
    programme = labelled_section(soup, 'Programme')
    description_parts = []
    if artists:
        description_parts.append(f'Artists\n{artists}')
    if programme:
        description_parts.append(f'Programme\n{programme}')

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(soup.select_one('p.time'))),
        'venue': venue,
        'city': city,
        'description': '\n\n'.join(description_parts) or None,
    }


def fetch(session, url):
    for attempt in range(3):
        response = session.get(url, timeout=45)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    response.raise_for_status()


class WyeValleyFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wyevalleyfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            sitemap_response = fetch(session, EVENT_SITEMAP_URL)
            sitemap = BeautifulSoup(sitemap_response.text, 'xml')
            urls = [clean_text(location) for location in sitemap.select('url > loc')]
            if not urls:
                raise ValueError('Event sitemap contained no event URLs')

            records = []
            for url in urls:
                try:
                    response = fetch(session, url)
                    record = parse_event(response.text, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Wye Valley event detail',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Wye Valley event catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=EVENT_SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    WyeValleyFestivalComCrawler().run()


if __name__ == '__main__':
    main()
