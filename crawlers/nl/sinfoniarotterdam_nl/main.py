import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfoniarotterdam.nl/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-concert-1.xml'
SOURCE = 'Sinfonia Rotterdam'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1, 'februari': 2, 'maart': 3, 'april': 4,
    'mei': 5, 'juni': 6, 'juli': 7, 'augustus': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def concert_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    return sorted({
        clean_text(node)
        for node in soup.select('url > loc')
        if '/concert/' in clean_text(node)
    })


def jsonld_events(soup):
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.get_text().lstrip('\ufeff \n\r\t')
        start = raw.find('{')
        if start < 0:
            continue
        try:
            payload = json.loads(raw[start:])
        except json.JSONDecodeError:
            continue
        items = payload.get('@graph', []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            items = [items]
        for item in items:
            item_type = item.get('@type') if isinstance(item, dict) else None
            types = item_type if isinstance(item_type, list) else [item_type]
            if 'Event' in types or 'MusicEvent' in types:
                events.append(item)
    return events


def page_description(soup):
    container = soup.select_one('.concert > .text, .concert .col-md-8.text')
    if not container:
        return None
    content = BeautifulSoup(str(container), 'html.parser')
    for node in content.select(
        'h1, .optreden_info, .prijzen, script, style, button, img, .gallery, '
        '.order-tickets, .fasc-button'
    ):
        node.decompose()
    return clean_text(content) or None


def parse_start(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def record_from_event(event, page_url, description):
    start = parse_start(event.get('startDate'))
    location = event.get('location') or {}
    address = location.get('address') or {} if isinstance(location, dict) else {}
    if not isinstance(address, dict):
        address = {}
    title = clean_text(event.get('name'))
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry')).upper() or 'NL'
    if len(country) != 2:
        country = 'NL'
    if not start or not title or not venue or not city:
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': page_url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country,
        'description': description or clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_dutch_occurrence(text):
    match = re.search(
        r'\b(?:ma|di|wo|do|vr|za|zo)\s+(\d{1,2})\s+'
        r'(' + '|'.join(MONTHS) + r'),?\s+(\d{4})',
        text.lower(),
    )
    if not match:
        return None, None
    try:
        date_value = datetime(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).date().isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', text)
    time_value = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return date_value, time_value


def split_location(value):
    if hasattr(value, 'select_one'):
        emphasized = value.select_one('strong')
        value = clean_text(emphasized or value)
    else:
        value = clean_text(value)
    value = next((line for line in value.splitlines() if line.strip()), '')
    for separator in (',', ' - '):
        if separator in value:
            venue, city = value.rsplit(separator, 1)
            if clean_text(venue) and clean_text(city):
                return clean_text(venue), clean_text(city)
    return None, None


def html_records(soup, page_url, description):
    title = clean_text(soup.select_one('.concert h1'))
    records = []
    for occurrence in soup.select('.concert .optreden'):
        date_value, time_value = parse_dutch_occurrence(clean_text(occurrence.select_one('.datum')))
        venue, city = split_location(occurrence.select_one('.locatie'))
        if not title or not date_value or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': date_value,
            'url': page_url,
            'time_from': time_value,
            'venue': venue,
            'city': city,
            'country_code': 'NL',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_detail(session, url):
    soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    description = page_description(soup)
    events = jsonld_events(soup)
    if events:
        return [
            record for event in events
            if (record := record_from_event(event, url, description))
        ]
    return html_records(soup, url, description)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['city']): item
        for item in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class SinfoniaRotterdamNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfoniarotterdam_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SinfoniaRotterdamNlCrawler().run()


if __name__ == '__main__':
    main()
