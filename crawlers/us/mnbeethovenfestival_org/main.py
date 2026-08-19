import html
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mnbeethovenfestival.org/'
SOURCE = 'Minnesota Beethoven Festival'
SCHEDULE_API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/pages')
ARTISTS_API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/artists')
CITY = 'Winona'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2}),\s*(\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m)\.?)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_occurrences(value, year):
    occurrences = []
    for month, day, time_value in DATE_TIME_RE.findall(clean_text(value)):
        try:
            event_date = datetime.strptime(
                f'{month} {day} {year}', '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue

        normalized_time = re.sub(r'\.', '', time_value).upper().replace('  ', ' ')
        time_from = None
        for pattern in ('%I:%M %p', '%I %p'):
            try:
                time_from = datetime.strptime(normalized_time, pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
        occurrences.append((event_date, time_from))
    return occurrences


def get_json(session, url, params):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def get_schedule(session):
    pages = get_json(
        session,
        SCHEDULE_API_URL,
        {
            'slug': 'festival-schedule',
            'per_page': 1,
            '_fields': 'link,title,content',
        },
    )
    if not pages:
        return None
    return pages[0]


def get_artist_descriptions(session):
    descriptions = {}
    page = 1
    while True:
        response = session.get(
            ARTISTS_API_URL,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'link,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        for artist in response.json():
            description = clean_text(artist.get('content', {}).get('rendered'))
            if description:
                descriptions[artist['link'].rstrip('/')] = description

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return descriptions


def schedule_year(schedule):
    title = clean_text(schedule.get('title', {}).get('rendered'))
    match = re.search(r'\b(20\d{2})\b', title)
    return int(match.group(1)) if match else None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    schedule = get_schedule(session)
    if not schedule:
        log_message(
            'Festival schedule page was not found',
            event='crawler_empty_listing',
            level='warning',
            url=SCHEDULE_API_URL,
            record_count=0,
        )
        return []

    year = schedule_year(schedule)
    if not year:
        log_message(
            'Festival schedule year could not be determined',
            event='crawler_parse_warning',
            level='warning',
            url=schedule['link'],
            error_type='MissingScheduleYear',
        )
        return []

    descriptions = get_artist_descriptions(session)
    soup = BeautifulSoup(schedule['content']['rendered'], 'html.parser')
    records = []
    seen_urls = set()

    for title_node in soup.find_all('h2'):
        link = title_node.find('a', href=True)
        if not link or '/festival-schedule/' not in link['href']:
            continue
        url = urljoin(SOURCE_URL, link['href']).rstrip('/')
        if url in seen_urls:
            continue

        card = title_node.find_parent(
            'div', class_=lambda value: value and 'fusion-builder-row-inner' in value
        )
        if not card:
            continue

        card_links = card.select('h2 a[href]')
        title_parts = []
        for card_link in card_links:
            if urljoin(SOURCE_URL, card_link['href']).rstrip('/') != url:
                continue
            part = clean_text(card_link)
            if part and part not in title_parts:
                title_parts.append(part)
        title = ' & '.join(title_parts)

        date_node = card.find('h3')
        venue_node = card.find('h4')
        venue = clean_text(venue_node)
        occurrences = parse_occurrences(date_node, year)
        if not title or not venue or not occurrences:
            continue

        summary_parts = []
        for paragraph in card.find_all('p'):
            text = clean_text(paragraph)
            if text and text not in summary_parts:
                summary_parts.append(text)
        detail = descriptions.get(url)
        if detail and detail not in summary_parts:
            summary_parts.append(detail)
        description = '\n\n'.join(summary_parts) or None

        for event_date, time_from in occurrences:
            records.append({
                'title': title,
                'date': event_date,
                'url': f'{url}/',
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        seen_urls.add(url)

    if not records:
        log_message(
            'No concerts were parsed from the festival schedule',
            event='crawler_empty_listing',
            level='warning',
            url=schedule['link'],
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MnBeethovenFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mnbeethovenfestival_org',
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
    MnBeethovenFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
