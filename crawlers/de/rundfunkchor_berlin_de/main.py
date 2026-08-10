import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urldefrag, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rundfunkchor-berlin.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender/')
SOURCE = 'Rundfunkchor Berlin'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}
COUNTRY_CODES = {
    'Deutschland': 'DE',
    'Schweiz': 'CH',
    'Österreich': 'AT',
    'Litauen': 'LT',
}
MONTHS = {
    'Jan': 1, 'Feb': 2, 'März': 3, 'Apr': 4, 'Mai': 5, 'Juni': 6,
    'Juli': 7, 'Aug': 8, 'Sep': 9, 'Okt': 10, 'Nov': 11, 'Dez': 12,
}
VENUE_CITY_DEFAULTS = {
    'Philharmonie Essen': 'Essen',
    'UdK Konzertsaal Hardenbergstraße': 'Berlin',
}


def clean_text(value, separator='\n'):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def parse_date(value, year):
    match = re.fullmatch(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\.?', value.strip())
    if not match:
        return None
    month = MONTHS.get(match.group(2).rstrip('.'))
    if not month:
        return None
    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(\d{1,2})(?:[.:](\d{2}))?\s*Uhr', value)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for month_section in soup.select('.Calendar-Month[data-month]'):
        match = re.fullmatch(r'(\d{4})-\d{1,2}', month_section.get('data-month', ''))
        if not match:
            continue
        year = int(match.group(1))
        for item in month_section.select('.ConcertListItem-Event'):
            link = item.select_one('.ConcertListItem-Event-Link[href]')
            day = item.select_one('.ConcertListItem-Event-Date-Day')
            title = clean_text(item.select_one('.ConcertListItem-Event-Info h3'), ' ')
            venue = clean_text(item.select_one('.ConcertListItem-Event-Info-Location'), ' ')
            event_date = parse_date(clean_text(day, ' '), year)
            if not link or not title or not venue or not event_date:
                continue
            time_tag = item.select_one('.ConcertListItem-Event-Date time:not(.ConcertListItem-Event-Date-Day)')
            events.append({
                'title': title,
                'date': event_date,
                'url': urldefrag(urljoin(SOURCE_URL, link['href']))[0],
                'time_from': parse_time(clean_text(time_tag, ' ')),
                'venue': venue,
            })
    return events


def address_location(section):
    heading = clean_text(section.select_one('h2'), ' ')
    address = section.select_one('.ProductionLocation-Text p:last-of-type')
    lines = [line for line in clean_text(address).splitlines() if line]
    country_code = COUNTRY_CODES.get(lines[-1], 'DE') if lines else 'DE'
    if lines and lines[-1] in COUNTRY_CODES:
        lines.pop()
    city = None
    for line in reversed(lines):
        postal = re.match(r'^(?:[A-Z]{1,2}-)?\d{4,5}\s+(.+)$', line)
        if postal:
            city = postal.group(1).strip()
            break
    return {'name': heading, 'city': city, 'country_code': country_code}


def parse_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    page_content = clean_text(soup.select_one('.ProductionInfo > .PageContent'))
    if page_content:
        parts.append(page_content)
    programme = clean_text(soup.select_one('.ProductionDetailsItem-part--program'))
    if programme:
        parts.append('Programm\n' + programme)
    return {
        'description': '\n\n'.join(parts) or None,
        'locations': [address_location(section) for section in soup.select('.ProductionLocation')],
    }


def matching_location(venue, locations):
    normalized = re.sub(r'[^a-z0-9]+', '', venue.casefold())
    best = None
    best_score = 0
    for location in locations:
        candidate = re.sub(r'[^a-z0-9]+', '', location['name'].casefold())
        score = min(len(normalized), len(candidate)) if candidate in normalized or normalized in candidate else 0
        if score > best_score:
            best, best_score = location, score
    return best


def enrich_details(url):
    try:
        return url, parse_detail(get_html(url))
    except requests.RequestException as error:
        log_message(
            'Failed to scrape concert detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return url, {'description': None, 'locations': []}


def get_concerts():
    events = parse_calendar(get_html(CALENDAR_URL))
    details = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(enrich_details, url) for url in {event['url'] for event in events}]
        for future in as_completed(futures):
            url, detail = future.result()
            details[url] = detail

    records = []
    for event in events:
        detail = details[event['url']]
        location = matching_location(event['venue'], detail['locations'])
        if location and not location['city'] and event['venue'] in VENUE_CITY_DEFAULTS:
            location = {**location, 'city': VENUE_CITY_DEFAULTS[event['venue']]}
        if not location or not location['city']:
            log_message(
                'Skipping concert with unresolved location',
                event='crawler_item_skipped',
                level='warning',
                url=event['url'],
                venue=event['venue'],
            )
            continue
        records.append({
            **event,
            'city': location['city'],
            'country_code': location['country_code'],
            'description': detail['description'],
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return sorted(records, key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['venue']
    ))


class RundfunkchorBerlinDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rundfunkchor_berlin_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RundfunkchorBerlinDeCrawler().run()


if __name__ == '__main__':
    main()
