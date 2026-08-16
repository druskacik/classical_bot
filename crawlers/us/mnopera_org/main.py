import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mnopera.org/'
SOURCE = 'Minnesota Opera'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/op_shows'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Referer': SOURCE_URL,
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\s+at\s+'
    r'(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    value = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    return re.sub(r'\s+', ' ', html.unescape(value).replace('\xa0', ' ')).strip()


def season_years(url):
    match = re.search(r'/season/(20\d{2})-(20\d{2})/', url)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def occurrence_year(month, explicit_year, url):
    if explicit_year:
        return int(explicit_year)
    years = season_years(url)
    if not years:
        return None
    return years[0] if month >= 7 else years[1]


def parse_city(address):
    if re.search(r'\bSt\.?\s*Paul\b', address, re.IGNORECASE):
        return 'St. Paul'
    if re.search(r'\bMinneapolis\b', address, re.IGNORECASE):
        return 'Minneapolis'
    return None


def parse_show_page(page, url, fallback_title):
    soup = BeautifulSoup(page, 'html.parser')
    info = soup.select_one('.singleshow__main__info')
    address = soup.select_one('.singleshow__sidebar__address')
    if info is None or address is None:
        return []

    title = clean_text(soup.select_one('h1.default__title')) or fallback_title
    info_text = clean_text(info)
    address_text = clean_text(address)
    city = parse_city(address_text)
    address_lines = [part.strip() for part in address.get_text('\n').splitlines() if part.strip()]
    venue = address_lines[0] if address_lines else ''
    if not city or not venue:
        return []

    records = []
    for match in DATE_RE.finditer(info_text):
        month = MONTHS[match.group(1).lower()]
        year = occurrence_year(month, match.group(3), url)
        if year is None:
            continue
        try:
            event_date = date(year, month, int(match.group(2))).isoformat()
        except ValueError:
            continue
        hour = int(match.group(4)) % 12
        if match.group(6).lower() == 'p':
            hour += 12
        time_from = f'{hour:02d}:{match.group(5) or "00"}'

        event_venue = venue
        event_city = city
        # This archived series explicitly assigns its first performance to
        # MacPhail and all remaining performances to the Luminary Arts Center.
        if url.endswith('/2024-2025/mnop-plus/'):
            if event_date == '2024-11-16':
                event_venue = 'MacPhail Center for Music'
            else:
                event_venue = 'Luminary Arts Center'
            event_city = 'Minneapolis'

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': event_venue,
            'city': event_city,
            'country_code': 'US',
            'description': info_text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MnoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mnopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch_catalogue(self):
        shows = []
        page_number = 1
        with requests.Session() as session:
            session.headers.update(HEADERS)
            while True:
                response = session.get(
                    API_URL,
                    params={'per_page': 100, 'page': page_number},
                    timeout=45,
                )
                if response.status_code == 400 and page_number > 1:
                    break
                response.raise_for_status()
                batch = response.json()
                shows.extend(batch)
                total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
                if page_number >= total_pages or len(batch) < 100:
                    break
                page_number += 1
        return shows

    def scrape(self):
        try:
            shows = self.fetch_catalogue()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Minnesota Opera show catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        season_ids = {
            show['id'] for show in shows
            if show.get('parent') == 0
            and re.search(r'20\d{2}.?[–-].?20\d{2}', show['title']['rendered'])
        }
        candidates = [
            show for show in shows
            if show.get('parent') in season_ids
            and not re.search(r'\b(?:gala|party)\b', show['title']['rendered'], re.IGNORECASE)
        ]

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(requests.get, show['link'], headers=HEADERS, timeout=45): show
                for show in candidates
            }
            for future in as_completed(futures):
                show = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    title = clean_text(BeautifulSoup(show['title']['rendered'], 'html.parser'))
                    records.extend(parse_show_page(response.text, show['link'], title))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Minnesota Opera show page',
                        event='crawler_fetch_failed',
                        level='warning',
                        url=show['link'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        unique = {}
        for record in records:
            key = (record['date'], record['time_from'], record['venue'], record['title'])
            unique[key] = record
        return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))


def main():
    MnoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
