import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.semperoper.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan.html')
RENDER_URL = urljoin(SOURCE_URL, 'spielplan')
SOURCE = 'Semperoper Dresden'
HEADERS = {
    'User-Agent': 'classical-concert-crawler/1.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}
LOCAL_VENUES = {
    'Semperoper Dresden', 'Semper Zwei', 'Kulturpalast Dresden',
    'Staatsschauspiel Dresden', 'Schauspielhaus Dresden',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u2009', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_text(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.text


def schedule_data(page):
    schedule_match = re.search(
        r'document\.NIS__SCHEDULE\s*=\s*(\[.*?\]);', page, re.S,
    )
    type_match = re.search(
        r'id="ni-pageloader-type-num"[^>]*data-pagenum="(\d+)"', page,
    )
    load_match = re.search(r'document\.NIS__LOAD_COUNT\s*=\s*["\']?(\d+)', page)
    if not schedule_match or not type_match:
        raise ValueError('Semperoper schedule index was not found')
    return (
        json.loads(schedule_match.group(1)),
        type_match.group(1),
        int(load_match.group(1)) if load_match else 15,
    )


def render_batch(session, page_type, load_count, batch):
    payload = {str(index): event['sospuid'] for index, event in batch}
    encoded = base64.b64encode(
        json.dumps(payload, separators=(',', ':')).encode('utf-8')
    ).decode('ascii')
    content = get_text(session, RENDER_URL, params={
        'mode': 'after',
        'type': page_type,
        'action': 'loadByScroll',
        'loadingCount': load_count,
        'loadingEvents': encoded,
    })
    fragments = json.loads(content)
    if isinstance(fragments, dict):
        fragments = [fragments[key] for key in sorted(fragments, key=int)]
    return [parse_fragment(fragment) for fragment in fragments]


def parse_fragment(fragment):
    soup = BeautifulSoup(fragment, 'html.parser')
    event = soup.select_one('.ni-schedule-event') or soup
    link = event.select_one('a[href*="/spielplan/stuecke/"]')
    moment = event.select_one('time[datetime]')
    venue = clean_text(event.select_one('.ni-event-venue .p2'))
    title = clean_text(event.select_one('.ni-bannertitle'))
    if not link or not moment or not title or not venue or venue == 'Gastspielort':
        return None
    try:
        parsed = datetime.fromisoformat(moment.get('datetime', ''))
    except ValueError:
        return None
    # The named houses in this institutional calendar are Dresden venues. Tour
    # entries use "Gastspielort" and are deliberately excluded above.
    if venue not in LOCAL_VENUES and 'Dresden' not in venue:
        return None
    subtitle = clean_text(event.select_one('.ni-subtitle'))
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': urljoin(SOURCE_URL, link.get('href')),
        'time_from': parsed.strftime('%H:%M'),
        'venue': venue,
        'city': 'Dresden',
        'country_code': 'DE',
        'description': subtitle or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def detail_description(session, url):
    soup = BeautifulSoup(get_text(session, url), 'html.parser')
    parts = []
    for selector in ('.ni-detail-headinfo-container', '.ni-production-synopsis'):
        for node in soup.select(selector):
            value = clean_text(node)
            if value and value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = make_session()
    page = get_text(session, SCHEDULE_URL)
    schedule, page_type, load_count = schedule_data(page)
    batches = [list(enumerate(schedule))[start:start + load_count + 1]
               for start in range(0, len(schedule), load_count + 1)]
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(render_batch, session, page_type, load_count, batch): batch
            for batch in batches
        }
        for future in as_completed(futures):
            try:
                records.extend(record for record in future.result() if record)
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                log_message(
                    'Failed to render Semperoper schedule batch',
                    event='crawler_page_failed', level='warning', url=RENDER_URL,
                    error_type=type(error).__name__, error_message=str(error),
                )

    descriptions = {}
    urls = sorted({detail_url(record['url']) for record in records})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to enrich Semperoper production',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    for record in records:
        long_description = descriptions.get(detail_url(record['url']))
        if long_description:
            record['description'] = '\n\n'.join(
                part for part in (record['description'], long_description) if part
            )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['venue'], item['title'], item['url'],
    ))


class SemperoperDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='semperoper_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SemperoperDeCrawler().run()


if __name__ == '__main__':
    main()
