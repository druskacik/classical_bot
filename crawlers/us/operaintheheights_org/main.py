import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaintheheights.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Opera in the Heights'
CITY = 'Houston'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?<!\d)(\d{1,2}/\d{1,2}/20\d{2})'
    r'(?:\s+(\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%m/%d/%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\s+', ' ', value.strip().upper())
    normalized = re.sub(r'(?<=\d)(AM|PM)$', r' \1', normalized)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def page_text(soup):
    main = soup.select_one('main') or soup
    return clean_text(main.get_text('\n', strip=True))


def parse_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    text = page_text(soup)
    marker = re.search(r'\bPERFORMANCE DATES\s*:?', text, re.IGNORECASE)
    if not marker:
        return []

    venue_marker = re.search(r'\nVENUE\s*:?[ \t]*\n', text[marker.end():], re.IGNORECASE)
    if not venue_marker:
        return []

    dates_text = text[marker.end():marker.end() + venue_marker.start()]
    # The archive includes on-demand films. They are not live performances.
    if re.search(r'\b(?:online|stream(?:ing)?|on-demand)\b', dates_text, re.IGNORECASE):
        return []

    venue_start = marker.end() + venue_marker.end()
    venue_text = text[venue_start:].split('\n', 1)[0].strip(' :-')
    if not venue_text or re.search(r'\b(?:online|stream(?:ing)?|on-demand)\b', venue_text, re.I):
        return []

    heading = soup.select_one('main h1, main h2, h1')
    title = clean_text(heading.get_text(' ', strip=True) if heading else '')
    if not title:
        return []

    description = clean_text(text[:marker.start()]) or None
    occurrences = []
    for match in DATE_TIME_RE.finditer(dates_text):
        event_date = parse_date(match.group(1))
        if not event_date:
            continue
        occurrences.append([event_date, parse_time(match.group(2))])

    # One archived page contains a single mistyped year among four performances
    # in the same month. Correct only that tightly constrained pattern.
    if len(occurrences) >= 3 and len({value[0][5:7] for value in occurrences}) == 1:
        year_counts = Counter(value[0][:4] for value in occurrences)
        dominant_year, dominant_count = year_counts.most_common(1)[0]
        if dominant_count >= 3:
            for value in occurrences:
                if year_counts[value[0][:4]] == 1:
                    value[0] = dominant_year + value[0][4:]

    records = []
    for event_date, time_from in occurrences:
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue_text,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def sitemap_page_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in soup.select('url > loc'):
        url = clean_text(node.get_text())
        parsed = urlparse(url)
        if parsed.netloc == 'www.operaintheheights.org' and parsed.path not in {'', '/'}:
            urls.append(url)
    return list(dict.fromkeys(urls))


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_page_urls(session)
    records = []

    def fetch(url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return parse_page(url, response.text)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Opera in the Heights page request failed',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No Opera in the Heights performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OperaintheheightsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaintheheights_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OperaintheheightsOrgCrawler().run()


if __name__ == '__main__':
    main()
