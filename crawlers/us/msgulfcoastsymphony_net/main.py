import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://msgulfcoastsymphony.net/'
EVENT_SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-event-1.xml'
SOURCE = 'Mississippi Gulf Coast Symphony'
VENUE_CITIES = {
    'Jones Park': 'Gulfport',
}

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
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    value = clean_text(value)
    for pattern in (
        '%I:%M%p, %A, %B %d, %Y',
        '%I%p, %A, %B %d, %Y',
        '%A, %B %d, %Y',
    ):
        try:
            parsed = datetime.strptime(value, pattern)
            time_from = parsed.strftime('%H:%M') if '%I' in pattern else None
            return parsed.date().isoformat(), time_from
        except ValueError:
            pass
    return None, None


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return [clean_text(node) for node in soup.find_all('loc') if '/event/' in clean_text(node)]


def description_from_page(soup):
    parts = []
    field_sets = soup.select('.vem-single-event-field-set')
    labels = ('Program', 'Featuring')
    for index, field_set in enumerate(field_sets):
        lines = []
        for field in field_set.select('.one-field'):
            key = clean_text(field.select_one('.field-set-key'))
            value = clean_text(field.select_one('.field-set-value'))
            line = ' '.join(item for item in (key, value) if item)
            if line:
                lines.append(line)
        if lines:
            label = labels[index] if index < len(labels) else 'Event details'
            parts.append(f"{label}\n" + '\n'.join(lines))

    details = clean_text(soup.select_one('.vem-single-event-details'))
    if details:
        parts.append(details)
    return '\n\n'.join(parts) or None


def title_from_page(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload.get('@graph', []) if isinstance(payload, dict) else payload
        if isinstance(candidates, dict):
            candidates = [candidates]
        if isinstance(payload, dict) and payload.get('@type') == 'Event':
            candidates = [payload]
        for candidate in candidates if isinstance(candidates, list) else []:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                title = clean_text(candidate.get('name'))
                if title:
                    return title
    return clean_text(soup.select_one('h1'))


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = title_from_page(soup)
    description = description_from_page(soup)
    records = []

    for occurrence in soup.select('.vem-one-occurrence'):
        event_date, time_from = parse_datetime(
            occurrence.select_one('.vem-single-event-date-start')
        )
        venue = clean_text(occurrence.select_one('.vem-single-occurrence-venue'))
        city_text = clean_text(occurrence.select_one('.venue-city'))
        city = clean_text(city_text.split(',')[0]) or VENUE_CITIES.get(venue, '')
        if not all((title, event_date, venue, city)):
            continue
        records.append({
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
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENT_SITEMAP_URL, timeout=45)
    response.raise_for_status()
    urls = event_urls(response.text)

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            page_records = parse_event_page(response.text, url)
            if not page_records:
                log_message(
                    'Event page had no valid occurrences',
                    event='crawler_event_skipped',
                    level='warning',
                    url=url,
                    record_count=0,
                )
            records.extend(page_records)
        except requests.RequestException as error:
            log_message(
                'Event page request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENT_SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MsGulfCoastSymphonyNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='msgulfcoastsymphony_net',
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
    MsGulfCoastSymphonyNetCrawler().run()


if __name__ == '__main__':
    main()
