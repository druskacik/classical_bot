import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfonietta.ch/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'saisons/')
SOURCE = 'Sinfonietta de Lausanne'

# The host rejects generic HTTP clients. These client-hint headers match a
# normal Chromium navigation and do not contain a session-specific value.
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Sec-CH-UA': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'Sec-CH-UA-Mobile': '?0',
    'Sec-CH-UA-Platform': '"Linux"',
    'Upgrade-Insecure-Requests': '1',
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.6',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'decembre': 12,
}

# The orchestra occasionally tours; these known non-Swiss cities must not be
# assigned the organization's home country.
FOREIGN_CITY_COUNTRIES = {
    'annecy': 'FR', 'besançon': 'FR', 'dijon': 'FR', 'evian': 'FR',
    'évian': 'FR', 'lyon': 'FR', 'paris': 'FR',
    'berlin': 'DE', 'fribourg-en-brisgau': 'DE', 'munich': 'DE',
    'münchen': 'DE', 'stuttgart': 'DE',
    'aoste': 'IT', 'milan': 'IT', 'milano': 'IT', 'turin': 'IT',
    'bruxelles': 'BE', 'londres': 'GB', 'london': 'GB',
}

DATE_RE = re.compile(
    r'\b(\d{1,2})\s+(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|'
    r'ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b([01]?\d|2[0-3])\s*(?:h|:)([0-5]\d)?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    month = MONTHS[match.group(2).casefold()]
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_location(line):
    parts = [part.strip(' -–—') for part in line.split(',') if part.strip(' -–—')]
    if len(parts) < 2:
        return None
    venue = ', '.join(parts[:-1])
    city = re.sub(r'^\d{4,6}\s+', '', parts[-1]).strip()
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    country_code = FOREIGN_CITY_COUNTRIES.get(city.casefold(), 'CH')
    return venue, city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if main is None:
        return None
    lines = [line for line in clean_text(main).splitlines() if line]

    date_index = next((i for i, line in enumerate(lines) if DATE_RE.search(line)), None)
    if date_index is None:
        return None
    event_date = parse_date(lines[date_index])
    location = None
    for line in lines[date_index + 1:date_index + 5]:
        location = parse_location(line)
        if location:
            break
    if not event_date or not location:
        return None

    title_node = soup.select_one('meta[property="og:title"]')
    title = clean_text(title_node.get('content')) if title_node else ''
    title = re.sub(r'\s*\|\s*Le Sinfonietta de Lausanne\s*$', '', title).strip()
    if not title:
        return None

    venue, city, country_code = location
    description = '\n'.join(lines[date_index + 2:]).strip() or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(lines[date_index]),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_html(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


class SinfoniettaChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonietta_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        archive_html = fetch_html(session, ARCHIVE_URL)
        archive_soup = BeautifulSoup(archive_html, 'html.parser')
        season_urls = list(dict.fromkeys(
            urljoin(SOURCE_URL, link['href'])
            for link in archive_soup.select('a[href*="/saisons/"][href]')
            if re.search(r'/saisons/20\d{2}-\d{2}/?$', link['href'])
        ))

        event_urls = []
        for season_url in season_urls:
            try:
                season_soup = BeautifulSoup(fetch_html(session, season_url), 'html.parser')
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Sinfonietta de Lausanne season archive',
                    event='crawler_item_failed', level='warning', url=season_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            event_urls.extend(
                urljoin(SOURCE_URL, link['href'])
                for link in season_soup.select('a[href*="/evenements/"][href]')
            )
        event_urls = list(dict.fromkeys(event_urls))

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_html, session, url): url for url in event_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_event(future.result(), url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Sinfonietta de Lausanne concert',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Sinfonietta de Lausanne concert with incomplete data',
                        event='crawler_item_skipped', level='warning', url=url,
                    )

        log_message(
            'Sinfonietta de Lausanne catalogue scraped',
            event='crawler_scrape_completed', record_count=len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


def main():
    SinfoniettaChCrawler().run()


if __name__ == '__main__':
    main()
