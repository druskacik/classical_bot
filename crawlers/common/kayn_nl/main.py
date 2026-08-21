import re
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kayn.nl/'
EVENTS_URL = f'{SOURCE_URL}events/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Roland Kayn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9,nl;q=0.7',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate((
        '', 'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December',
    ))
    if name
}
MONTHS.update({name[:3].lower(): number for name, number in MONTHS.items()})


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,\s*(20\d{2})\b',
        text,
        re.I,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))
        ).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b(\d{1,2})(?:[.:](\d{2}))\s*(am|pm)?\b', text, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    suffix = (match.group(3) or '').lower()
    if minute > 59 or hour > (12 if suffix else 23) or hour == 0 and suffix:
        return None
    if suffix == 'pm' and hour != 12:
        hour += 12
    elif suffix == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def resolve_location(title, text):
    combined = f'{title}\n{text}'
    # The archive uses prose rather than location fields. These identifiers are
    # explicit venue/city evidence in its entries, not home-location defaults.
    if re.search(r'\bZKM\b', combined) and re.search(r'\bKarlsruhe\b', combined, re.I):
        return 'ZKM', 'Karlsruhe', 'DE'
    if re.search(r'Gleishalle\s*\+\s*Spedition', combined, re.I) and re.search(
        r'\bBremen\b', combined, re.I
    ):
        return 'Gleishalle + Spedition', 'Bremen', 'DE'
    if re.search(r'CentQuatre(?:-Paris)?', combined, re.I) and re.search(
        r'\bParis\b', combined, re.I
    ):
        return 'CentQuatre-Paris', 'Paris', 'FR'
    return None, None, None


def external_event_url(group):
    for link in group.select('.x-accordion-inner a[href]'):
        href = link.get('href', '').strip()
        host = urlparse(href).hostname or ''
        if href.startswith(('http://', 'https://')) and host not in {'kayn.nl', 'www.kayn.nl'}:
            return href
    return EVENTS_URL


def parse_events(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for group in soup.select('.x-accordion-group'):
        heading = group.select_one('.x-accordion-heading')
        body = group.select_one('.x-accordion-inner')
        title = clean_text(heading)
        description = clean_text(body)
        event_date = parse_date(description)
        venue, city, country_code = resolve_location(title, description)
        url = external_event_url(group)
        if not title or not event_date or not venue or not city or not country_code or not url:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(description),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class KaynNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kayn_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            API_URL,
            params={
                'slug': 'events',
                '_fields': 'content,link',
                'per_page': 1,
            },
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        pages = response.json()
        if not pages:
            log_message(
                'Kayn events page was not returned by the WordPress API',
                event='crawler_source_empty',
                level='warning',
                url=API_URL,
                record_count=0,
            )
            return []
        html = (pages[0].get('content') or {}).get('rendered') or ''
        records = parse_events(html)
        log_message(
            'Parsed Kayn event archive',
            event='crawler_archive_parsed',
            url=EVENTS_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    KaynNlCrawler().run()


if __name__ == '__main__':
    main()
