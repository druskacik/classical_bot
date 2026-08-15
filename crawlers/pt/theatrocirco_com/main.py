import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://theatrocirco.com/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programa/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'arquivo/')
SOURCE = 'Theatro Circo'
DEFAULT_VENUE = 'Theatro Circo'
DEFAULT_CITY = 'Braga'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.6',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    lines = [' '.join(line.split()) for line in value.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def event_links(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    return {
        urljoin(SOURCE_URL, anchor['href'])
        for anchor in soup.select('a[href*="/event/"]')
        if '/en/event/' not in anchor.get('href', '')
    }


def archive_years(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    return sorted({
        button['data-year']
        for button in soup.select('button.year_button[data-year]')
        if button['data-year'].isdigit()
    })


def parse_event(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    heading = soup.select_one('section.title-top h1')
    title = clean_text(heading)
    if not title:
        return []

    popup = soup.select_one('[data-sessions]')
    try:
        sessions = json.loads(html.unescape(popup['data-sessions'])) if popup else []
    except (json.JSONDecodeError, TypeError):
        sessions = []

    info_spans = soup.select('section.title-top .info-box > div:first-child > span')
    venue = clean_text(info_spans[1]) if len(info_spans) > 1 else DEFAULT_VENUE
    if not venue:
        venue = DEFAULT_VENUE

    description_parts = []
    for section in soup.select('section.text.module'):
        text = clean_text(section)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for session in sessions:
        raw_start = session.get('start') if isinstance(session, dict) else None
        try:
            start = datetime.strptime(raw_start, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            continue
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': DEFAULT_CITY,
            'description': description,
        })
    return records


class TheatroCircoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theatrocirco_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, url):
        response = session.get(url, timeout=60)
        response.raise_for_status()
        return response.text

    def _session(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET',),
        )
        session.mount('https://', HTTPAdapter(max_retries=retries))
        return session

    def scrape(self):
        session = self._session()
        try:
            programme_html = self._get(session, PROGRAMME_URL)
            archive_html = self._get(session, ARCHIVE_URL)
            links = event_links(programme_html) | event_links(archive_html)
            year_urls = [f'{ARCHIVE_URL}?ano={year}' for year in archive_years(archive_html)]
            with ThreadPoolExecutor(max_workers=5) as executor:
                for year_html in executor.map(lambda url: self._get(session, url), year_urls):
                    links.update(event_links(year_html))
        except requests.RequestException as error:
            log_message(
                'Failed to load Theatro Circo programme index',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAMME_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        failures = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(self._get, self._session(), url): url
                for url in sorted(links)
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_event(future.result(), url))
                except requests.RequestException as error:
                    failures.append((url, error))

        if failures:
            url, error = failures[0]
            log_message(
                'Some Theatro Circo event pages could not be loaded',
                event='crawler_partial_fetch_failure',
                level='warning',
                url=url,
                failure_count=len(failures),
                error_type=type(error).__name__,
                error_message=str(error),
            )
        if not records:
            raise ValueError('No parseable event sessions found')
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    TheatroCircoComCrawler().run()


if __name__ == '__main__':
    main()
