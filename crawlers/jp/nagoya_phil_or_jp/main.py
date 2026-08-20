import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nagoya-phil.or.jp/'
CALENDAR_API = f'{SOURCE_URL}apps/get_calendar.php'
SOURCE = 'Nagoya Philharmonic Orchestra'
START_YEAR = 2015

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.7',
}

VENUE_CITIES = {
    '愛知県芸術劇場': '名古屋市',
    '日本特殊陶業市民会館': '名古屋市',
    'Niterra日本特殊陶業市民会館': '名古屋市',
    '岡谷鋼機名古屋公会堂': '名古屋市',
    '名古屋市公会堂': '名古屋市',
    'しらかわホール': '名古屋市',
    '電気文化会館': '名古屋市',
    'アイプラザ豊橋': '豊橋市',
    '豊田市コンサートホール': '豊田市',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.json()


def get_html(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def fetch_month(year, month):
    payload = get_json(
        CALENDAR_API,
        {'c_year': year, 'c_month': f'{month:02d}'},
    )
    return [item['event'] for item in payload.get('data') or [] if item.get('event')]


def listing_events():
    months = [
        (year, month)
        for year in range(START_YEAR, date.today().year + 3)
        for month in range(1, 13)
    ]
    events = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_month, *month): month for month in months}
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                events.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_API}?c_year={year}&c_month={month:02d}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return events


def venue_from_event(event):
    info = event.get('informations') or {}
    place = info.get('concerts_informations_place')
    if isinstance(place, dict):
        venue = clean_text(place.get('label'))
    else:
        venue = clean_text(place)
    return clean_text(info.get('concerts_informations_place_free')) or venue


def city_from_event(event, venue):
    evidence = f'{venue}\n{clean_text(event.get("title"))}'
    for name, city in VENUE_CITIES.items():
        if name in evidence:
            return city

    # Japanese venue and performance names commonly carry their municipality.
    # Retain the administrative suffix to avoid confusing a district with a city.
    matches = re.findall(r'([\u4e00-\u9fffヶ]{2,10}(?:市|区|町|村))', evidence)
    excluded = {'市民会館'}
    matches = [match for match in matches if match not in excluded]
    return matches[-1] if matches else None


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    for section in soup.select('main .contVox'):
        heading = section.find(['h2', 'h3'])
        label = clean_text(heading.get_text(' ', strip=True)) if heading else ''
        if label in {'料金', 'お問合せ', 'チケット', '備考'}:
            continue
        text = clean_text(section.get_text('\n', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def make_records(event, html=None):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date = clean_text(event.get('event_date'))
    venue = venue_from_event(event)
    city = city_from_event(event, venue)
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return []
    if not title or not url or not venue or not city:
        return []

    time_text = clean_text((event.get('informations') or {}).get('concerts_informations_time'))
    times = re.findall(r'(?<!\d)([0-2]?\d:[0-5]\d)', time_text)
    times = list(dict.fromkeys(f'{int(value.split(":")[0]):02d}:{value[-2:]}' for value in times))
    if not times:
        times = [None]
    description = detail_description(html) if html else None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in times
    ]


def get_concerts():
    events = listing_events()
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_html, event['url']): event
            for event in events
            if event.get('url')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(make_records(event, future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('url'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                records.extend(make_records(event))
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class NagoyaPhilOrJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nagoya_phil_or_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
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
        return get_concerts()


def main():
    NagoyaPhilOrJpCrawler().run()


if __name__ == '__main__':
    main()
