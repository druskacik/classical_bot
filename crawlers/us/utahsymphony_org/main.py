import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://utahsymphony.org/'
SCHEDULE_URL = f'{SOURCE_URL}schedule/'
SOURCE = 'Utah Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Abravanel Hall': 'Salt Lake City',
    'Maurice Abravanel Hall': 'Salt Lake City',
    'Janet Quinney Lawson Capitol Theatre': 'Salt Lake City',
    'UVU Noorda Center for the Performing Arts': 'Orem',
    'UVU’s Noorda Center for the Performing Arts': 'Orem',
    'UVU Noorda Center': 'Orem',
    'The Noorda at UVU': 'Orem',
    'Browning Center at WSU, Austad Auditorium': 'Ogden',
    'Browning Center at WSU': 'Ogden',
    'Daines Concert Hall at the Chase Fine Arts Center': 'Logan',
    'Chase Fine Arts Center': 'Logan',
    'BYU Concert Hall - School of Music Building': 'Provo',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def detail_description(session, url):
    soup = get_soup(session, url)
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Some legacy event pages put an HTML address containing unescaped
            # quotes in otherwise useful JSON-LD. The description itself is
            # still consistently bounded by the following image property.
            match = re.search(
                r'"description"\s*:\s*"(.*?)"\s*,\s*"image"\s*:',
                raw or '',
                flags=re.DOTALL,
            )
            if match:
                description = clean_text(match.group(1).replace(r'\n', '\n'))
                if description:
                    return description
            continue
        if isinstance(data, dict) and data.get('@type') == 'MusicEvent':
            description = clean_text(data.get('description'))
            if description:
                return description
    return None


def parse_occurrence(text):
    parts = [clean_text(part) for part in text.split('|')]
    if len(parts) < 3:
        return None
    try:
        event_date = datetime.strptime(parts[0], '%A, %B %d, %Y').date().isoformat()
        event_time = datetime.strptime(parts[1], '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None
    return event_date, event_time, parts[-1]


def listing_items(soup):
    items = []
    for node in soup.select('li.event-grid'):
        title_node = node.select_one('.title')
        link = title_node.find_parent('a') if title_node else None
        title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
        url = link.get('href', '').strip() if link else ''
        description = clean_text(node.get('data-description')) or None
        if not title or not url:
            continue
        items.append((node, title, url, description))
    return items


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, SCHEDULE_URL)
    items = listing_items(soup)

    descriptions = {}
    missing_urls = {url for _, _, url, description in items if not description}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, session, url): url
            for url in missing_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for node, title, url, description in items:
        description = description or descriptions.get(url)
        for occurrence in node.select('.show-time li'):
            parsed = parse_occurrence(occurrence.get_text(' ', strip=True))
            if not parsed:
                continue
            event_date, event_time, venue = parsed
            city = VENUE_CITIES.get(venue)
            if not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': event_time,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class UtahsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='utahsymphony_org',
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
        return get_concerts()


def main():
    UtahsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
