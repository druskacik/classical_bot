import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sinfonia.cymru/'
SITEMAP_URL = f'{SOURCE_URL}programme-sitemap.xml'
SOURCE = 'Sinfonia Cymru'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+'
    r'(20\d{2})'
    r'(?:\s+(\d{1,2}):(\d{2})\s*(am|pm))?\b',
    re.IGNORECASE,
)

# Older occurrence rows sometimes name only the venue. These are stable,
# well-known venue/locality pairs observed in the first-party archive.
VENUE_CITIES = {
    'Aberystwyth Arts Centre': 'Aberystwyth',
    'Amgueddfa Ceredigion Museum': 'Aberystwyth',
    'Canolfan y Celfyddydau Aberystwyth Arts Centre': 'Aberystwyth',
    'Coffee @ Upcycle': 'Chepstow',
    'Criccieth Memorial Hall': 'Criccieth',
    'Gregynog Hall': 'Tregynon',
    'Llancarfan Community Centre': 'Llancarfan',
    'Neuadd Llan Ffestiniog Hall': 'Llan Ffestiniog',
    'Pentredwr Community Centre (Old School)': 'Pentredwr',
    'Pontyberem Memorial Hall': 'Pontyberem',
    "Porter's Cardiff": 'Cardiff',
    'Royal Welsh College of Music & Drama': 'Cardiff',
    'Rhosygilwen': 'Cilgerran',
    "St David's Hall": 'Cardiff',
    "St. David's Hall": 'Cardiff',
    'Symphony Hall': 'Birmingham',
    'The Bridgewater Hall': 'Manchester',
    'Theatr Brycheiniog': 'Brecon',
    'Tŷ Cemaes': 'Cemaes',
    'Venue Cymru': 'Llandudno',
    'Wyeside Arts Centre': 'Builth Wells',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def programme_urls():
    soup = BeautifulSoup(get_response(SITEMAP_URL).content, 'xml')
    urls = []
    for node in soup.select('url > loc'):
        url = clean_text(node)
        if '/programme/' not in url or '/cy/' in url or '/wp-content/' in url:
            continue
        urls.append(url)
    return list(dict.fromkeys(urls))


def parse_date_time(text):
    match = DATE_TIME_RE.search(text)
    if not match:
        return None, None
    day, month, year, hour, minute, meridiem = match.groups()
    try:
        event_date = datetime.strptime(f'{day} {month} {year}', '%d %b %Y').date().isoformat()
    except ValueError:
        return None, None

    if hour is None:
        return event_date, None
    hour_number = int(hour)
    minute_number = int(minute)
    if not 1 <= hour_number <= 12 or minute_number > 59:
        return event_date, None
    if hour_number == 12:
        hour_number = 0
    if meridiem.lower() == 'pm':
        hour_number += 12
    # Midnight is used by this site for occurrences whose time is unknown.
    time_from = None if (hour_number, minute_number) == (0, 0) else f'{hour_number:02d}:{minute_number:02d}'
    return event_date, time_from


def parse_venue_city(value):
    value = clean_text(value)
    if not value:
        return None, None
    if ',' in value:
        venue, city = value.rsplit(',', 1)
        city = city.strip()
        if '|' in city:
            city = city.rsplit('|', 1)[-1].strip()
        return venue.strip() or None, city or None
    city = VENUE_CITIES.get(value)
    return (value, city) if city else (None, None)


def extract_description(soup):
    content = soup.select_one('main.programme .siteContent-main')
    if not content:
        return None
    parts = []
    for node in content.find_all(recursive=False):
        classes = node.get('class') or []
        if node.name == 'header' or 'share-root' in classes:
            continue
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_programme(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('main.programme h1'))
    if not title:
        return []
    description = extract_description(soup)
    records = []
    for occurrence in soup.select('main.programme li.programme-event'):
        venue, city = parse_venue_city(occurrence.select_one('.programme-event-venue'))
        event_date, time_from = parse_date_time(clean_text(occurrence))
        if not event_date or not venue or not city:
            continue
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    urls = programme_urls()
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_response, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_programme(future.result().content, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Sinfonia Cymru programme detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class SinfoniaCymruCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonia_cymru',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
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
        return get_concerts()


def main():
    SinfoniaCymruCrawler().run()


if __name__ == '__main__':
    main()
