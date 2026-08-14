import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfoniasmithsq.org.uk/'
FEED_URL = f'{SOURCE_URL}wp-content/uploads/whats-on.json'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Sinfonia Smith Square'
CITY = 'London'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
TIME_RE = re.compile(r'\b([01]?\d)(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_json(session):
    response = session.get(FEED_URL, timeout=60)
    response.raise_for_status()
    return response.json()


def event_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return list(
        dict.fromkeys(
            clean_text(node)
            for node in soup.select('url > loc')
            if '/event/' in clean_text(node)
        )
    )


def normalise_time(text):
    match = TIME_RE.search(text or '')
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def dates_from_attribute(value):
    values = (value or '').split('/')
    try:
        start = date.fromisoformat(values[0][:10])
        end = date.fromisoformat(values[-1][:10])
    except (ValueError, IndexError):
        return []
    if end < start or (end - start).days > 31:
        return [start.isoformat()]
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def description_from_page(article):
    parts = []
    selectors = (
        '.section--post-details .post-details--group_large',
        '.article-content .content--intro',
        '.article-content .content--text',
        '.section--repertoire',
    )
    for selector in selectors:
        for node in article.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(content, url, instances=None):
    soup = BeautifulSoup(content, 'html.parser')
    article = soup.select_one('main article')
    if not article:
        return []
    title = clean_text(article.select_one('h1'))
    venue = clean_text(article.select_one('.article-header .location'))
    time_node = article.select_one('.article-header time')
    if not title or not venue or venue.lower() == 'elsewhere' or not time_node:
        return []

    description = description_from_page(article)
    if instances:
        occurrences = instances
    else:
        event_dates = dates_from_attribute(time_node.get('datetime'))
        event_time = normalise_time(clean_text(time_node))
        occurrences = [(event_date, event_time) for event_date in event_dates]

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'GB',
            'description': description,
        }
        for event_date, time_from in occurrences
    ]


def current_instances(feed):
    result = {}
    for item in feed.get('items', []):
        url = item.get('link')
        instances = []
        for value in item.get('timestamp') or []:
            try:
                parsed = datetime.strptime(value, '%Y-%m-%d %H:%M')
            except (TypeError, ValueError):
                continue
            instances.append((parsed.date().isoformat(), parsed.strftime('%H:%M')))
        if url and instances:
            result[url] = instances
    return result


class SinfoniaSmithSquareCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfoniasmithsq_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = make_session()
        feed = get_json(session)
        instances_by_url = current_instances(feed)
        urls = list(dict.fromkeys([*event_urls(session), *instances_by_url]))
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=60): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_detail(response.content, url, instances_by_url.get(url)))
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Sinfonia Smith Square event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SinfoniaSmithSquareCrawler().run()


if __name__ == '__main__':
    main()
