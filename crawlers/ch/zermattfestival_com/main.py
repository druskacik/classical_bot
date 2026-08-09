import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://zermattfestival.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concerts'
SOURCE = 'Zermatt Music Festival & Academy'

HEADERS = {
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'decembre': 12,
}

# These catalogue entries are ancillary activities rather than performances.
NON_CONCERT_TITLES = {
    'ateliers scolaires',
    'master classe',
    'open master class',
    'pre-concert talk',
    'titre du concert',
    'zermatt music festival – special menu',
    'zermatt music festival - special menu',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value, fallback_year):
    match = re.search(
        r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)(?:\s+(\d{4}))?\b', value, re.IGNORECASE
    )
    if not match:
        return None
    month = MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3) or fallback_year), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*[hH.:]\s*([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(value, title):
    raw = clean_text(value)
    if not raw:
        return None

    # Older pages put prices, ticket instructions, or full addresses in the
    # same heading as the venue. Stop before those additions.
    venue = re.split(
        r'\s+(?:CHF\b|ENTR[ÉE]E?\b|BILLETS?\b|TICKETS?\b|\+41\b|'
        r'GEWERBESTRASSE\b|SCHULHAUSSTRASSE\b|OBERE MATTENSTRASSE\b)',
        raw, maxsplit=1, flags=re.IGNORECASE,
    )[0].strip(' ,-')
    venue = re.sub(r'\s+\d{4}\s+.*$', '', venue).strip(' ,-')
    if not venue:
        return None

    evidence = f'{title} {raw}'.casefold()
    if 'gianadda' in evidence or 'martigny' in evidence:
        city = 'Martigny'
    elif 'martinsheim' in evidence or 'visp' in evidence or 'viège' in evidence:
        city = 'Visp'
    elif 'st niklaus' in evidence or 'st. niklaus' in evidence or 'sankt nikolaus' in evidence:
        city = 'St. Niklaus'
    else:
        city = 'Zermatt'
    return venue, city


def fetch_catalogue(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'asc'},
            timeout=60,
        )
        response.raise_for_status()
        events.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', 1)):
            break
        page += 1

    # The API contains parallel French, English, and German posts. The French
    # canonical posts have no language prefix and provide the fullest archive.
    return [
        event for event in events
        if '/en/concerts/' not in event.get('link', '')
        and '/de/concerts/' not in event.get('link', '')
    ]


def parse_event(event, page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    main = soup.select_one('.elementor-location-single')
    if main is None:
        return None
    headings = [clean_text(node) for node in main.select('.elementor-widget-heading')]
    headings = [item for item in headings if item]
    if len(headings) < 4:
        return None

    title = headings[0]
    if title.casefold() in NON_CONCERT_TITLES:
        return None
    published = event.get('date', '')
    fallback_year = published[:4] if re.match(r'^\d{4}', published) else ''
    event_date = parse_date(headings[1], fallback_year)
    location = parse_location(headings[3], title)
    url = event.get('link', '')
    if not all((title, event_date, location, url)):
        return None

    venue, city = location
    programme = clean_text(main.select_one('.elementor-widget-theme-post-content'))
    description_parts = [programme] if programme else []
    # Artist credits immediately following the programme are useful context
    # for later programme extraction, without including ticket boilerplate.
    labels = {'artistes', 'artistes invité·es', 'artistes invitées', 'künstler*innen', 'artists'}
    for index, heading in enumerate(headings):
        if heading.casefold().rstrip(':') in labels and index + 1 < len(headings):
            description_parts.append(headings[index + 1])
            break

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(headings[2]),
        'venue': venue,
        'city': city,
        'country_code': 'CH',
        'description': '\n\n'.join(dict.fromkeys(description_parts)) or None,
        '_published': published,
    }


class ZermattfestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='zermattfestival_com',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_catalogue(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Zermatt Festival catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(session.get, event['link'], timeout=60): event
                for event in events if event.get('link')
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_event(event, response.text)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to process Zermatt Festival concert',
                        event='crawler_item_failed',
                        level='warning',
                        url=event.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        # A small number of redesigned pages remain in WordPress alongside an
        # older version of the same performance. Keep the newest publication.
        deduplicated = {}
        for record in sorted(records, key=lambda item: item['_published'], reverse=True):
            key = (
                record['title'].casefold(), record['date'], record['time_from'],
                record['city'].casefold(),
            )
            deduplicated.setdefault(key, record)
        records = list(deduplicated.values())
        for record in records:
            record.pop('_published', None)

        log_message(
            'Zermatt Festival catalogue scraped',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    ZermattfestivalComCrawler().run()


if __name__ == '__main__':
    main()
