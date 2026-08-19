import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicinsalisbury.org.uk/'
SOURCE = 'Music in Salisbury'
CALENDAR_API = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if isinstance(value, Tag):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_urls(session):
    # WP FullCalendar returns event post URLs, but its timestamps on this site
    # are publication timestamps. Detail pages remain authoritative for dates.
    response = session.get(
        CALENDAR_API,
        params={
            'action': 'WP_FullCalendar',
            'start': '2000-01-01',
            'end': '2100-01-01',
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    return {
        item.get('url')
        for item in payload
        if item.get('post_id') and item.get('url') and '?event=' in item['url']
    }


def labelled_paragraph(container, label):
    for paragraph in container.find_all('p', recursive=False):
        strong = paragraph.find('strong')
        if strong and clean_text(strong).rstrip(':').lower() == label.lower():
            return paragraph
    return None


def parse_city(container):
    map_container = container.select_one('.em-osm-container')
    if map_container is None:
        return None
    scripts = ' '.join(script.get_text(' ', strip=True) for script in map_container.select('script'))
    match = re.search(r"bindPopup\('(?:<br/>)?(.+?)(?:<br/>)?<a ", scripts)
    if not match:
        return None
    address = re.sub(r'<[^>]+>', ' ', match.group(1))
    address = clean_text(address.replace("\\'", "'").replace('\\/', '/'))
    if ' - ' in address:
        city = address.rsplit(' - ', 1)[1]
    else:
        city = address.rsplit(',', 1)[-1]
    city = re.sub(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b.*$', '', city, flags=re.I)
    city = city.strip(' ,.-')
    normalised = {
        'salisbury': 'Salisbury',
        'saisbury': 'Salisbury',
        'hindon, salisbury': 'Hindon',
    }
    return normalised.get(city.lower(), city) or None


def parse_description(container):
    parts = []
    for child in container.children:
        if not isinstance(child, Tag):
            continue
        if child.select_one('.em-osm-container') or 'em-osm-container' in (child.get('class') or []):
            break
        strong = child.find('strong')
        if strong and clean_text(strong).rstrip(':').lower() in {
            'date/time', 'location', 'ticket prices', 'tickets available from',
            'member', 'event contact',
        }:
            break
        text = clean_text(child)
        if text:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('.em-event-single')
    title = clean_text(soup.select_one('h1.entry-title, h1'))
    if not container or not title:
        return None, set()

    neighbours = {
        urljoin(SOURCE_URL, link['href'])
        for link in container.select('nav.post-navigation a[href*="?event="]')
    }

    date_paragraph = labelled_paragraph(container, 'Date/Time')
    location_paragraph = labelled_paragraph(container, 'Location')
    date_text = clean_text(date_paragraph)
    match = re.search(
        r'\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b', date_text
    )
    months = {
        month.lower(): number for number, month in enumerate(
            ('', 'January', 'February', 'March', 'April', 'May', 'June',
             'July', 'August', 'September', 'October', 'November', 'December')
        ) if month
    }
    try:
        event_date = date(
            int(match.group(3)), months[match.group(2).lower()], int(match.group(1))
        ).isoformat() if match else None
    except (KeyError, ValueError):
        event_date = None

    times = re.findall(r'\b(\d{1,2}):([0-5]\d)\s*([ap]m)\b', date_text, re.I)
    time_from = None
    if times:
        hour, minute, meridiem = times[0]
        hour = int(hour) % 12 + (12 if meridiem.lower() == 'pm' else 0)
        time_from = f'{hour:02d}:{minute}'

    venue_link = location_paragraph.select_one('a') if location_paragraph else None
    venue = clean_text(venue_link)
    city = parse_city(container)
    if not all((event_date, venue, city)):
        return None, neighbours

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': parse_description(container),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }, neighbours


def fetch_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, response.url)


class MusicInSalisburyOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicinsalisbury_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )
        session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=12))
        try:
            pending = calendar_urls(session)
        except (requests.RequestException, json.JSONDecodeError) as error:
            log_message(
                'Failed to discover Music in Salisbury events',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        seen = set()
        records = []
        while pending:
            batch = pending - seen
            if not batch:
                break
            seen.update(batch)
            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = {executor.submit(fetch_event, session, url): url for url in batch}
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        record, neighbours = future.result()
                    except (requests.RequestException, ValueError) as error:
                        log_message(
                            'Failed to scrape Music in Salisbury event',
                            event='crawler_item_failed',
                            level='warning',
                            url=url,
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )
                        continue
                    pending.update(neighbours)
                    if record:
                        records.append(record)

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    MusicInSalisburyOrgUkCrawler().run()


if __name__ == '__main__':
    main()
