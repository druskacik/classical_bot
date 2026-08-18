import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.californiasymphony.org/'
SHOWS_API = f'{SOURCE_URL}wp-json/wp/v2/shows'
SOURCE = 'California Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
    'Referer': SOURCE_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def list_shows(session):
    page = 1
    shows = []
    while True:
        response = get_response(
            session,
            SHOWS_API,
            params={
                'page': page,
                'per_page': 100,
                '_fields': 'id,link,slug,title',
            },
        )
        batch = response.json()
        shows.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return shows


def parse_datetime(value):
    value = clean_text(value)
    for pattern in ('%A, %b %d, %Y %I:%M %p', '%A, %B %d, %Y %I:%M %p'):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None, None


def parse_location(soup):
    location = soup.select_one('.location')
    if not location:
        return None, None

    venue_node = location.find(['strong', 'h2', 'h3'])
    venue = clean_text(venue_node) if venue_node else ''
    location_text = clean_text(location)
    if not venue and location_text and len(location_text) <= 160:
        venue = location_text
    if not venue and 'Lesher Center for the Arts' in location_text:
        venue = 'Lesher Center for the Arts'

    city = None
    city_match = re.search(r',\s*([^,\n]+),\s*CA\s+\d{5}(?:-\d{4})?\b', location_text)
    if city_match:
        city = clean_text(city_match.group(1))
    elif venue and 'Lesher Center for the Arts' in location_text:
        city = 'Walnut Creek'
    elif venue and venue.startswith('Walnut Creek '):
        city = 'Walnut Creek'

    return venue or None, city


def parse_show(show, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.find('h1')) or clean_text((show.get('title') or {}).get('rendered'))
    url = show.get('link') or ''
    venue, city = parse_location(soup)
    if not title or not url or not venue or not city:
        return []

    article = soup.select_one('article.main')
    description = clean_text(article) or None
    records = []
    for node in soup.select('.showDateTime'):
        event_date, time_from = parse_datetime(node.get_text(' ', strip=True))
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    shows = list_shows(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_response, session, show['link']): show
            for show in shows if show.get('link')
        }
        for future in as_completed(futures):
            show = futures[future]
            try:
                records.extend(parse_show(show, future.result().text))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=show.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class CaliforniaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='californiasymphony_org',
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
    CaliforniaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
