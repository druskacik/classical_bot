import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://theconrad.org/'
SOURCE = 'La Jolla Music Society at The Conrad'
SITEMAP_URL = f'{SOURCE_URL}sitemap-posttype-event.xml'
CALENDAR_URL = f'{SOURCE_URL}admin/wp-admin/admin-ajax.php'

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
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            )
        ),
    )
    return session


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    urls = []
    for node in soup.select('loc'):
        url = clean_text(node)
        if re.fullmatch(r'https://theconrad\.org/events/[^/]+/', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def calendar_occurrences(session):
    response = get_response(
        session,
        CALENDAR_URL,
        params={'action': 'basethemeCalendarMonthListRequest'},
    )
    months = response.json().get('months', [])
    occurrences = {}
    for month in months:
        value = month.get('value', '')
        if not re.fullmatch(r'\d{4}-\d{2}', value):
            continue
        response = get_response(
            session,
            CALENDAR_URL,
            params={
                'action': 'basethemeCalendarRequest',
                'viewType': 'dayGridMonth',
                'start': f'{value}-01T00:00:00Z',
            },
        )
        for item in response.json():
            soup = BeautifulSoup(item.get('title', ''), 'html.parser')
            link = soup.select_one('a.c-cal-instance__link[href]')
            venue = clean_text(soup.select_one('.c-venue__name'))
            try:
                start = datetime.strptime(item.get('start', ''), '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                continue
            if not link or not venue:
                continue
            url = link['href'].split('?', 1)[0]
            occurrences.setdefault(url, []).append(
                {
                    'date': start.date().isoformat(),
                    'time_from': start.strftime('%H:%M'),
                    'venue': venue,
                }
            )
    return occurrences


def resolve_city(venue):
    normalized = venue.casefold()
    la_jolla_markers = (
        'baker-baum', 'the jai', 'atkinson room', 'wu tsai', 'the conrad',
        'st. james', 'st james', 'la jolla', 'scripps', 'sherwood auditorium',
    )
    san_diego_markers = (
        'jacobs music center', 'copley symphony hall', 'balboa theatre',
        'civic theatre', 'san diego', 'spreckels', 'the rady shell',
    )
    if any(marker in normalized for marker in la_jolla_markers):
        return 'La Jolla'
    if any(marker in normalized for marker in san_diego_markers):
        return 'San Diego'
    return None


def masthead_occurrence(soup):
    text = clean_text(soup.select_one('.c-event-masthead__date'))
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),\s+(\d{4})\s*[•·|]\s*(\d{1,2}(?::\d{2})?\s*[AP]M)',
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    value = ' '.join(match.groups())
    try:
        start = datetime.strptime(value, '%B %d %Y %I:%M %p')
    except ValueError:
        try:
            start = datetime.strptime(value, '%B %d %Y %I %p')
        except ValueError:
            return None
    return {'date': start.date().isoformat(), 'time_from': start.strftime('%H:%M')}


def page_description(soup):
    parts = []
    for node in soup.select('main .c-col-text-area.c-wysiwyg, main .c-container-titles.c-wysiwyg'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(url, content, api_occurrences=None):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('.c-event-masthead h1, main h1'))
    page_venue = clean_text(
        soup.select_one('.c-event-masthead__venue .c-venue__title')
        or soup.select_one('.c-event-masthead__venue .c-venue__name')
    )
    occurrences = api_occurrences or []
    if not occurrences:
        occurrence = masthead_occurrence(soup)
        if occurrence and page_venue:
            occurrence['venue'] = page_venue
            occurrences = [occurrence]
    description = page_description(soup)
    records = []
    for occurrence in occurrences:
        venue = occurrence.get('venue') or page_venue
        city = resolve_city(venue) if venue else None
        if not title or not venue or not city:
            continue
        records.append(
            {
                'title': title,
                'date': occurrence['date'],
                'url': url,
                'time_from': occurrence.get('time_from'),
                'venue': venue,
                'city': city,
                'description': description,
            }
        )
    return records


def get_concerts():
    session = build_session()
    urls = event_urls(session)
    try:
        occurrences = calendar_occurrences(session)
    except (requests.RequestException, ValueError) as error:
        occurrences = {}
        log_message(
            'Failed to retrieve The Conrad calendar API; using detail-page dates',
            event='crawler_calendar_failed',
            level='warning',
            url=CALENDAR_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(url, future.result().content, occurrences.get(url)))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape The Conrad event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class TheConradOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theconrad_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheConradOrgCrawler().run()


if __name__ == '__main__':
    main()
