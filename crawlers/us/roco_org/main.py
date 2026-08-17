import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://roco.org/'
SOURCE = 'ROCO'
PERFORMANCES_URL = urljoin(SOURCE_URL, 'performances/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'performance-archive/')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]{3,9})\.?\s+(\d{1,2})\s+(20\d{2}),\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?',
    re.I,
)


def clean_text(value, separator=' '):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def make_session():
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


def get_html(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def performance_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href')).split('#', 1)[0]
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split('/') if part]
        if parsed.netloc == 'roco.org' and len(parts) == 2 and parts[0] == 'performances':
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_occurrences(value):
    occurrences = []
    for match in DATE_RE.finditer(clean_text(value)):
        month, day, year, hour, minute, meridiem = match.groups()
        try:
            event_date = datetime.strptime(
                f'{month[:3]} {day} {year}', '%b %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
        occurrences.append((event_date, f'{hour:02d}:{int(minute or 0):02d}'))
    return occurrences


def city_from_map(url):
    if not url:
        return ''
    path = unquote(urlparse(url).path).replace('+', ' ')
    match = re.search(r',\s*([^,]+),\s*[A-Z]{2}(?:\s+\d{5})?(?:,|$)', path)
    return clean_text(match.group(1)) if match else ''


def event_location(soup):
    location = soup.select_one('main .entry-meta .location')
    venue = clean_text(location)
    city = city_from_map(location.select_one('a[href]').get('href')) if location and location.select_one('a[href]') else ''

    if venue and city:
        return venue, city

    for heading in soup.select('main .entry-content h4'):
        if clean_text(heading).casefold() != 'location':
            continue
        block = heading.find_next_sibling()
        if not block:
            continue
        lines = [clean_text(part) for part in block.stripped_strings]
        link = block.select_one('a[href*="google.com/maps"]')
        city = city or city_from_map(link.get('href')) if link else city
        if not city:
            match = re.search(r'\b([^,\n]+),\s*[A-Z]{2}\s+\d{5}\b', clean_text(block))
            city = clean_text(match.group(1)) if match else ''
        if not venue:
            venue = next((line for line in lines if not re.search(r'\d', line) and 'map' not in line.lower()), '')
        break
    return venue, city


def description_text(soup):
    content = soup.select_one('main .entry-content')
    if not content:
        return None
    body = BeautifulSoup(str(content), 'html.parser')
    for node in body.select('script, style, form, .addthisevent-drop, img'):
        node.decompose()
    text = body.get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('main article h1.entry-title, main article h1')
    date_node = soup.select_one('main .entry-meta .date')
    title = clean_text(title_node)
    occurrences = parse_occurrences(date_node)
    venue, city = event_location(soup)
    if not title or not occurrences or not venue or not city:
        return []

    description = description_text(soup)
    return [
        {
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
        }
        for event_date, time_from in occurrences
    ]


class RocoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='roco_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        index_urls = (PERFORMANCES_URL, ARCHIVE_URL)
        detail_urls = []
        for index_url in index_urls:
            html = get_html(session, index_url)
            detail_urls.extend(performance_urls(html))
        detail_urls = list(dict.fromkeys(detail_urls))

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(get_html, session, url): url for url in detail_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result(), url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch ROCO performance detail',
                        event='crawler_detail_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        log_message(
            'ROCO performance pages parsed',
            event='crawler_scrape_completed',
            url=PERFORMANCES_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    RocoOrgCrawler().run()


if __name__ == '__main__':
    main()
