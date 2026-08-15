import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://arizonachambermusic.org/'
SOURCE = 'Arizona Friends of Chamber Music'
SITEMAP_URL = f'{SOURCE_URL}afcm_post_events-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def music_event_data(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'MusicEvent':
                return candidate
    return None


def parse_start(value):
    if not isinstance(value, str):
        return None
    normalized = re.sub(r'T(\d):', r'T0\1:', value.replace('Z', '+00:00'))
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_detail(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    event = music_event_data(soup)
    title = clean_text(soup.select_one('#singleEvent h1.event-title'))
    event = event or {}
    title = title or str(event.get('name') or '').strip()
    title = re.sub(r'\s+', ' ', title).strip()
    start = parse_start(event.get('startDate'))

    location = event.get('location') if isinstance(event.get('location'), dict) else {}
    address = location.get('address') if isinstance(location.get('address'), dict) else {}
    venue = str(location.get('name') or '').strip()
    city = str(address.get('addressLocality') or '').strip()
    country_code = str(address.get('addressCountry') or '').strip().upper()

    subtitle = soup.select_one('#singleEvent .event-subtitle')
    subtitle_text = clean_text(subtitle)
    date_match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2},\s+20\d{2}',
        subtitle_text,
        re.IGNORECASE,
    )
    time_match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([ap]m)\b', subtitle_text, re.IGNORECASE)
    if not start:
        if date_match:
            try:
                parsed_date = datetime.strptime(date_match.group(0), '%B %d, %Y').date().isoformat()
            except ValueError:
                parsed_date = None
            if parsed_date:
                parsed_time = None
                if time_match:
                    parsed_time = datetime.strptime(
                        ''.join(time_match.groups()), '%I%M%p'
                    ).strftime('%H:%M')
                start = parsed_date, parsed_time
    elif time_match:
        # The site's JSON-LD drops the am/pm marker (for example 7:30pm is
        # serialized as T7:30), so the visible first-party time is authoritative.
        start = start[0], datetime.strptime(
            ''.join(time_match.groups()), '%I%M%p'
        ).strftime('%H:%M')
    if not venue and subtitle:
        venue_link = subtitle.select_one('a[href*="maps"]')
        venue = clean_text(venue_link)

    # AFCM's event calendar is for its Tucson concert-presenting programme.
    # Touring biography text does not change the occurrence's location.
    if venue and not city:
        city = 'Tucson'
    if city and not country_code:
        country_code = 'US'

    if not all((title, start, venue, city, country_code)):
        return None
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None

    description = clean_text(soup.select_one('#singleEvent .text-serif')) or None
    return {
        'title': title,
        'date': start[0],
        'url': url,
        'time_from': start[1],
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = location.get_text(strip=True)
        parsed = urlparse(url)
        if parsed.netloc == 'arizonachambermusic.org' and parsed.path.startswith('/events/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


class ArizonaChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arizonachambermusic_org',
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
        try:
            response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Arizona chamber music event sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = event_urls(response.text)
        records = []
        failed_count = 0

        def fetch(url):
            detail = requests.get(url, headers=HEADERS, timeout=45)
            detail.raise_for_status()
            return parse_detail(url, detail.text)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch, url): url for url in urls}
            for future in as_completed(futures):
                try:
                    record = future.result()
                except (requests.RequestException, ValueError):
                    failed_count += 1
                    continue
                if record:
                    records.append(record)

        if failed_count:
            log_message(
                'Some Arizona chamber music event pages could not be fetched',
                event='crawler_partial_fetch',
                level='warning',
                url=SITEMAP_URL,
                failed_count=failed_count,
                event_count=len(urls),
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ArizonaChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
