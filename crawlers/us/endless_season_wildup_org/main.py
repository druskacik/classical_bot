import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://endless-season.wildup.org/'
SOURCE = 'Wild Up — Endless Season'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'

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
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%A, %B %d, %Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_times(value):
    if not value or 'to be announced' in value.lower():
        return [None]

    matches = re.findall(r'\b(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.m\.|p\.m\.|am|pm)\b', value, re.I)
    times = []
    for match in matches:
        normalized = re.sub(r'\.', '', match.lower()).replace(' ', '')
        if ':' not in normalized:
            normalized = re.sub(r'(?=[ap]m$)', ':00', normalized)
        parsed = datetime.strptime(normalized, '%I:%M%p').strftime('%H:%M')
        if parsed not in times:
            times.append(parsed)
    return times or [None]


def parse_city(venue, address):
    location = f'{venue} {address}'.strip()
    match = re.search(r'(?:^|,\s*)([^,|]+),\s*[A-Z]{2}(?:\s+\d{5})?\s*$', location)
    if match:
        return match.group(1).strip()
    return None


def build_description(soup):
    sections = []
    for selector in ('.program', '.about_program'):
        text = clean_text(soup.select_one(selector))
        if text:
            sections.append(text)
    return '\n\n'.join(sections) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.event-content_title'))
    event_date = parse_date(clean_text(soup.select_one('.events-details__date .start-date')))
    venue = clean_text(soup.select_one('.events-details__location__name'))
    address = clean_text(soup.select_one('.events-details__location__address'))
    city = parse_city(venue, address)

    # A city-only festival listing is not a defensible venue and is skipped.
    if not title or not event_date or not venue or not address or not city:
        return []

    description = build_description(soup)
    start_time_elements = soup.select(
        '.events-details__time__start, .events-details__time .set--start'
    )
    start_time_text = '\n'.join(clean_text(element) for element in start_time_elements)
    times = parse_times(start_time_text)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        }
        for time_from in times
    ]


class EndlessSeasonWildupOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='endless_season_wildup_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
            response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=45)
            response.raise_for_status()
            posts = response.json()
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            for page in range(2, total_pages + 1):
                page_response = session.get(
                    API_URL, params={'per_page': 100, 'page': page}, timeout=45
                )
                page_response.raise_for_status()
                posts.extend(page_response.json())
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Endless Season event API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            url = post.get('link')
            if not url:
                continue
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Endless Season event detail',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_event(detail_response.text, url))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    EndlessSeasonWildupOrgCrawler().run()


if __name__ == '__main__':
    main()
