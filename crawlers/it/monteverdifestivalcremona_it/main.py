import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.monteverdifestivalcremona.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/evento'
SOURCE = 'Monteverdi Festival Cremona'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

DATE_PATTERN = re.compile(r'\b(\d{2}/\d{2}/\d{4})\b')
TIME_PATTERN = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)\b')


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'link'},
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        urls.extend(item['link'] for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def location_from_text(text):
    parts = [part.strip() for part in text.split(',') if part.strip()]
    if not parts:
        return None
    venue = ', '.join(parts[:-1]) if len(parts) > 1 else parts[0]
    city = parts[-1] if len(parts) > 1 else 'Cremona'
    if not venue or not city:
        return None
    return venue, city


def event_occurrences(soup):
    label = soup.find(string=re.compile(r"Data e luogo dell.evento", re.I))
    if label is None:
        return []
    details = label.find_parent(class_='wp-block-group')
    if details is None:
        return []

    date_items = []
    venue_items = []
    for item in details.select('li'):
        text = clean_text(item)
        match = DATE_PATTERN.search(text)
        if match:
            time_match = TIME_PATTERN.search(text)
            date_items.append((match.group(1), time_match))
        elif text and text.casefold() != clean_text(label).casefold():
            venue_items.append(text)

    if not date_items or not venue_items:
        return []
    if len(venue_items) == 1:
        venue_items *= len(date_items)
    if len(venue_items) != len(date_items):
        return []

    occurrences = []
    for (date_text, time_match), location_text in zip(date_items, venue_items):
        try:
            event_date = datetime.strptime(date_text, '%d/%m/%Y').date().isoformat()
        except ValueError:
            continue
        location = location_from_text(location_text)
        if location is None:
            continue
        time_from = None
        if time_match:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        occurrences.append((event_date, time_from, *location))
    return occurrences


def parse_event(soup, url):
    title_node = (
        soup.select_one('main .gspb-dynamic-title-element')
        or soup.select_one('main h1')
        or soup.select_one('h1')
    )
    title = clean_text(title_node)
    if not title:
        return []

    content = soup.select_one('main .entry-content') or soup.select_one('main')
    description = clean_text(content)
    description = re.split(r"Data e luogo dell.evento", description, maxsplit=1, flags=re.I)[0]
    if description.casefold().startswith(title.casefold()):
        description = description[len(title):].strip()

    records = []
    for event_date, time_from, venue, city in event_occurrences(soup):
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'IT',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MonteverdiFestivalCremonaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='monteverdifestivalcremona_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        try:
            urls = get_event_urls(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Monteverdi Festival event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_event(BeautifulSoup(response.content, 'html.parser'), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Monteverdi Festival event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    MonteverdiFestivalCremonaItCrawler().run()


if __name__ == '__main__':
    main()
