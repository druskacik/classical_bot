import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://moravskedivadlo.cz/cs'
SOURCE = 'Moravské divadlo Olomouc'
AJAX_URL = f'{SOURCE_URL}/_ajax/filtered/{{page}}'
HOME_CITY = 'Olomouc'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.7',
    'Referer': f'{SOURCE_URL}/program',
    'X-Requested-With': 'XMLHttpRequest',
}


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def node_text(parent, selector):
    node = parent.select_one(selector)
    return clean_text(node.get_text('\n', strip=True)) if node else ''


def get_response(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response


def get_listing(session):
    articles = []
    for page in range(100):
        payload = get_response(session, AJAX_URL.format(page=page)).json()
        content = payload.get('content')
        if not isinstance(content, str):
            raise ValueError(f'Invalid programme response on page {page}')
        articles.extend(BeautifulSoup(content, 'html.parser').select('article.show'))
        if not payload.get('remainder'):
            return articles
    raise RuntimeError('Programme pagination exceeded 100 pages')


def parse_day_month(value):
    match = re.search(r'(\d{1,2})\.\s*(\d{1,2})\.', clean_text(value))
    if not match:
        return None
    return tuple(map(int, match.groups()))


def dated_articles(articles, today=None):
    """Attach years to the site's chronological day/month-only programme."""
    today = today or date.today()
    parsed = []
    year = today.year
    previous_month = None

    for article in articles:
        day_month = parse_day_month(node_text(article, '.dt'))
        if not day_month:
            continue
        day, month = day_month
        if previous_month is not None and month < previous_month:
            year += 1
        try:
            event_date = date(year, month, day)
        except ValueError:
            continue

        # The endpoint normally begins with upcoming events. This also handles
        # a late-December scrape whose first returned programme is in January.
        if not parsed and event_date < today and month < today.month:
            year += 1
            event_date = date(year, month, day)
        previous_month = month
        parsed.append((article, event_date.isoformat()))
    return parsed


def resolve_location(value):
    venue = clean_text(value)
    if not venue or re.search(r'\b(mimo\s+olomouc|zájezd)\b', venue, re.IGNORECASE):
        return None, None
    if re.search(r'Brodek\s+u\s+Pv\.?', venue, re.IGNORECASE):
        return venue, 'Brodek u Prostějova'
    return venue, HOME_CITY


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', clean_text(value))
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def detail_description(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    synopsis = soup.select_one('main section.synopsis .sh-content.text')
    if not synopsis:
        return None
    for unwanted in synopsis.select('script, style, form, img, button'):
        unwanted.decompose()
    return clean_text(synopsis.get_text('\n', strip=True)) or None


def listing_records(articles):
    records = []
    for article, event_date in dated_articles(articles):
        link = article.select_one('h2.title a[href]')
        title = clean_text(link.get_text(' ', strip=True)) if link else ''
        venue, city = resolve_location(node_text(article, '.auditorium'))
        if not link or not title or not venue or not city:
            continue

        description_parts = [
            node_text(article, '.authors'),
            node_text(article, '.subtitle'),
        ]
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': urljoin(SOURCE_URL, link.get('href')),
                'time_from': parse_time(node_text(article, '.time')),
                'venue': venue,
                'city': city,
                'country_code': 'CZ',
                'description': clean_text('\n'.join(filter(None, description_parts))) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = listing_records(get_listing(session))

    descriptions = {}
    urls = sorted({record['url'] for record in records})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record['url']) or record['description']
    return records


class MoravskeDivadloCzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='moravskedivadlo_cz',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CZ',
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
    MoravskeDivadloCzCrawler().run()


if __name__ == '__main__':
    main()
