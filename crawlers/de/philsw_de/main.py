import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.philsw.de/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/kalender'
SOURCE = 'Philharmonie Südwestfalen'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_calendar_posts(session):
    params = {
        'per_page': 100,
        'page': 1,
        'status': 'publish',
        '_fields': 'link,title,content',
    }
    response = session.get(API_URL, params=params, timeout=45)
    response.raise_for_status()
    posts = response.json()
    total_pages = int(response.headers.get('X-WP-TotalPages', '1'))

    for page in range(2, total_pages + 1):
        params['page'] = page
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        posts.extend(response.json())
    return posts


def icon_value(soup, icon_class):
    icon = soup.select_one(f'.elementor-icon-list-item i.{icon_class}')
    if not icon:
        return ''
    item = icon.find_parent(class_='elementor-icon-list-item')
    text = item.select_one('.elementor-icon-list-text') if item else None
    return clean_text(text)


def parse_location(value):
    value = clean_text(value)
    if not value:
        return None, None
    # The location widget may append street and postal-address lines. Only
    # its first line contains the documented city/venue pair.
    value = value.splitlines()[0].strip()

    if '//' in value:
        # The site uses this reversed form for the KulturPur tent, e.g.
        # "Großes Zelttheater // Auf dem Giller bei Hilchenbach-Lützel".
        venue = clean_text(value.replace('//', ' / '))
        match = re.search(r'\bbei\s+([^/\n,]+)$', value, re.IGNORECASE)
        return (venue, clean_text(match.group(1))) if match else (None, None)

    parts = re.split(r'\s+/\s+|\s*,\s*', value, maxsplit=1)
    if len(parts) != 2:
        return None, None
    city, venue = (clean_text(part) for part in parts)
    if not city or not venue or city.casefold() == venue.casefold():
        return None, None
    return venue, city


def make_record(post, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text((post.get('title') or {}).get('rendered'))
    url = post.get('link') or ''

    date_text = icon_value(soup, 'fa-calendar-day')
    try:
        event_date = datetime.strptime(date_text, '%d.%m.%Y').date().isoformat()
    except (TypeError, ValueError):
        return None

    time_text = icon_value(soup, 'fa-clock')
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', time_text)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None

    venue, city = parse_location(icon_value(soup, 'fa-map-marker'))
    if not title or not url or not venue or not city:
        return None

    description = clean_text((post.get('content') or {}).get('rendered')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(post):
    url = post.get('link') or ''
    if not url:
        return None
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return make_record(post, response.text)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    posts = get_calendar_posts(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_record, post): post for post in posts}
        for future in as_completed(futures):
            post = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=post.get('link') or '',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class PhilswDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philsw_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    PhilswDeCrawler().run()


if __name__ == '__main__':
    main()
