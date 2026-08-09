import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.swissorchestra.ch/'
SOURCE = 'Swiss Orchestra'
LISTING_URLS = (
    f'{SOURCE_URL}konzerte-tickets',
    f'{SOURCE_URL}fruehere-tours-konzerte',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}
TIMEZONE = ZoneInfo('Europe/Zurich')
FOREIGN_CITY_COUNTRIES = {
    'Donostia - San Sebastiàn': 'ES',
    'Donostia - San Sebastián': 'ES',
    'Madrid': 'ES',
    'Köln': 'DE',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(url):
    last_error = None
    for _ in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            if '<html' in response.text.lower() and 'Just a moment...' not in response.text:
                return response.text
            last_error = requests.RequestException('Response did not contain the requested page')
        except requests.RequestException as error:
            last_error = error
    raise last_error


def listing_urls():
    urls = set()
    for listing_url in LISTING_URLS:
        soup = BeautifulSoup(get_html(listing_url), 'html.parser')

        def collect(page_soup):
            found = set()
            for link in page_soup.select('a[href^="/konzerte/"]'):
                path = link.get('href', '').split('?', 1)[0].strip()
                if path:
                    found.add(f'{SOURCE_URL.rstrip("/")}{path}')
            return found

        urls.update(collect(soup))
        # Webflow uses a distinct query parameter for each paginated CMS
        # collection. Advance each collection independently until it yields no
        # event URL that has not appeared on an earlier page.
        page_parameters = set()
        for link in soup.select('a.w-pagination-next[href]'):
            page_parameters.update(parse_qs(urlparse(link.get('href')).query))
        for parameter in page_parameters:
            for page_number in range(2, 50):
                page_url = f'{listing_url}?{parameter}={page_number}'
                page_soup = BeautifulSoup(get_html(page_url), 'html.parser')
                found = collect(page_soup)
                if not found or found.issubset(urls):
                    break
                urls.update(found)
    return sorted(urls)


def event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'MusicEvent':
                return candidate
    return None


def section_text(soup, heading):
    node = soup.find(
        ['h2', 'h3', 'h4', 'h5'],
        string=lambda value: value and clean_text(value).casefold() == heading.casefold(),
    )
    if not node:
        return ''
    wrapper = node.parent
    body = wrapper.select_one('.w-richtext') if wrapper else None
    return clean_text(body) if body else ''


def make_description(soup, data):
    parts = []
    summary = clean_text(data.get('description'))
    if summary:
        parts.append(summary)
    for heading in ('Über das Programm', 'Konzertprogramm'):
        body = section_text(soup, heading)
        if body and body not in parts:
            parts.append(f'{heading}\n{body}')
    return '\n\n'.join(parts) or None


def parse_start(value):
    if not isinstance(value, str) or not value.strip():
        return None, None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None, None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TIMEZONE)
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    data = event_data(soup)
    data = data or {}
    location = data.get('location') or {}
    address = location.get('address') or {}
    title = clean_text(data.get('name'))
    date, time_from = parse_start(data.get('startDate'))
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper()
    country_code = FOREIGN_CITY_COUNTRIES.get(city, country_code)

    # The four concerts from the orchestra's inaugural 2019 tour predate the
    # site's JSON-LD. Their detail templates still publish all required data.
    if not data:
        title_node = soup.select_one('h1.heading-style-h1')
        venue_node = soup.select_one('h2.heading-style-h1.text-color-white')
        city_node = soup.select_one('h2.heading-style-h1.text-span-highlight')
        title = clean_text(title_node)
        venue = clean_text(venue_node)
        city = clean_text(city_node)
        page_text = clean_text(soup)
        match = re.search(
            r'\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s+(\d{1,2}:\d{2})\b',
            page_text,
        )
        if match:
            day, month, year, time_from = match.groups()
            year = f'20{year}' if len(year) == 2 else year
            try:
                date = datetime(int(year), int(month), int(day)).date().isoformat()
            except ValueError:
                date = None
        country_code = 'CH' if city in {'Bern', 'Genf', 'St. Gallen', 'Zürich'} else ''
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        country_code = ''

    if not all((title, date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': make_description(soup, data),
    }


def scrape_concerts():
    urls = listing_urls()
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_html, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = make_record(url, future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Swiss Orchestra concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Swiss Orchestra concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class SwissOrchestraChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='swissorchestra_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SwissOrchestraChCrawler().run()


if __name__ == '__main__':
    main()
