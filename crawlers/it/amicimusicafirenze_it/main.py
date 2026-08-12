import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://amicimusicafirenze.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Amici della Musica di Firenze'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(text):
    text = re.sub(r'^Luogo\s*', '', clean_text(text), flags=re.I).strip(' ,')
    if not text:
        return None

    parts = [part.strip() for part in text.split(',') if part.strip()]
    if len(parts) == 1:
        return parts[0], 'Firenze'

    # MEC locations are normally rendered as "venue, city".  Preserve commas
    # inside a venue name and take the final component as the municipality.
    city = parts[-1]
    venue = ', '.join(parts[:-1])
    if not venue or not city or re.search(r'\d{5}|\b(?:via|viale|piazza)\b', city, re.I):
        return None
    return venue, city


def parse_event(post, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    date_node = soup.select_one('.mec-single-event-date .mec-start-date-label')
    if date_node is None:
        date_node = soup.select_one('.mec-single-event-date')
    date_match = re.search(r'\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b', clean_text(date_node))
    if not date_match:
        return None

    months = {
        'gen': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mag': 5, 'giu': 6,
        'lug': 7, 'ago': 8, 'set': 9, 'ott': 10, 'nov': 11, 'dic': 12,
    }
    try:
        month = months[date_match.group(2).casefold()]
        event_date = date(
            int(date_match.group(3)), month, int(date_match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None

    location_node = soup.select_one('.mec-single-event-location')
    location = parse_location(location_node)
    if not location:
        return None

    time_node = soup.select_one('.mec-single-event-time .mec-start-time')
    if time_node is None:
        time_node = soup.select_one('.mec-single-event-time')
    time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', clean_text(time_node))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None

    title = clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser'))
    description = clean_text(BeautifulSoup(post['content']['rendered'], 'html.parser')) or None
    venue, city = location
    if not title or not post.get('link'):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': post['link'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AmicimusicafirenzeItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicimusicafirenze_it',
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

    def _posts(self, session):
        posts = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={
                    'page': page,
                    'per_page': 100,
                    'status': 'publish',
                    'orderby': 'id',
                    'order': 'asc',
                },
                timeout=45,
            )
            response.raise_for_status()
            batch = response.json()
            posts.extend(batch)
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                return posts
            page += 1

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            posts = self._posts(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Amici della Musica event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []

        def fetch(post):
            response = session.get(post['link'], timeout=45)
            response.raise_for_status()
            return parse_event(post, response.text)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, post): post for post in posts}
            for future in as_completed(futures):
                post = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, KeyError, TypeError, ValueError) as error:
                    log_message(
                        'Failed to parse Amici della Musica event',
                        event='crawler_item_failed',
                        level='warning',
                        url=post.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AmicimusicafirenzeItCrawler().run()


if __name__ == '__main__':
    main()
