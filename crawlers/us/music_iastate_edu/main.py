import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.music.iastate.edu/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
PAST_EVENTS_URL = urljoin(SOURCE_URL, 'events/past')
SOURCE = 'Iowa State University Department of Music and Theatre'
CITY = 'Ames'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(card):
    month = clean_text(card.select_one('.isu-event-time_start_month'))
    day = clean_text(card.select_one('.isu-event-time_start_day'))
    year = clean_text(card.select_one('.isu-event-time_start_year'))
    try:
        return datetime.strptime(f'{month} {day} {year}', '%b %d %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(card):
    value = clean_text(card.select_one('.isu-event-time_start_time')).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def parse_listing_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for card in soup.select('main article.isu-feature'):
        link = card.select_one('.isu-feature_title a[href]')
        venue = clean_text(card.select_one('.isu-event-location-name_sm span'))
        if not venue:
            # Some theatre records omit the location field but repeat the venue
            # as its own final paragraph. Keep this deliberately narrow so prose
            # and addresses cannot become venue values.
            venue_pattern = re.compile(
                r'^[\w& .\'’@-]+(?:Theat(?:er|re)|Hall|Auditorium|Bandshell|Center|Centre)$',
                re.IGNORECASE,
            )
            candidates = [clean_text(node) for node in card.select('p')]
            venue = next((value for value in reversed(candidates) if venue_pattern.fullmatch(value)), '')
        if not link or not venue:
            continue

        title = clean_text(link)
        event_date = parse_date(card)
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if not title or not event_date or not url.startswith(SOURCE_URL):
            continue

        body = card.select_one('.isu-body')
        if body is None:
            feature_text = card.select_one('.isu-feature_text')
            paragraphs = feature_text.select('p') if feature_text else []
            description = '\n\n'.join(clean_text(node) for node in paragraphs if clean_text(node))
        else:
            description = clean_text(body)

        events.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(card),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    next_link = soup.select_one('li.pager__item--next a[href], a[rel="next"][href]')
    return events, next_link['href'] if next_link else None


def parse_detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    return clean_text(soup.select_one('article.node--type-event .isu-body')) or None


def fetch_listing(session, start_url):
    records = []
    seen_pages = set()
    url = start_url
    while url and url not in seen_pages:
        seen_pages.add(url)
        response = session.get(url, timeout=45)
        response.raise_for_status()
        page_records, next_url = parse_listing_page(response.text)
        records.extend(page_records)
        url = urljoin(url, next_url) if next_url else None
    return records


def enrich_descriptions(records, workers=8):
    def fetch(record):
        session = make_session()
        try:
            response = session.get(record['url'], timeout=45)
            response.raise_for_status()
            return parse_detail_description(response.text)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_records = {executor.submit(fetch, record): record for record in records}
        for future in as_completed(future_records):
            record = future_records[future]
            try:
                detail = future.result()
                if detail:
                    record['description'] = detail
            except requests.RequestException as error:
                log_message(
                    'Could not retrieve event detail; using listing description',
                    event='crawler_detail_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )


def scrape_concerts(session=None, enrich=True):
    session = session or make_session()
    records = []
    for start_url in (EVENTS_URL, PAST_EVENTS_URL):
        records.extend(fetch_listing(session, start_url))

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    records = list(unique.values())

    if enrich:
        enrich_descriptions(records)
    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicIastateEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_iastate_edu',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MusicIastateEduCrawler().run()


if __name__ == '__main__':
    main()
