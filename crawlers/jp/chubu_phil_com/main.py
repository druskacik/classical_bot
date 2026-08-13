import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chubu-phil.com/'
SOURCE = 'Chubu Philharmonic Orchestra'
CALENDAR_API = f'{SOURCE_URL}apps/get_calendar.php'
DETAIL_URL = f'{SOURCE_URL}concert/detail/{{event_id}}'
FIRST_ARCHIVE_YEAR = 2013

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The calendar supplies venue taxonomy terms but not municipalities. Most names
# contain their municipality; this table covers both those names and halls whose
# familiar names omit it. Explicit tour venues are therefore never assigned the
# orchestra's home city.
CITY_HINTS = {
    '小牧': 'Komaki',
    '愛知県芸術劇場': 'Nagoya',
    'しらかわホール': 'Nagoya',
    'サラマンカホール': 'Gifu',
    'クラギ文化ホール': 'Matsusaka',
    '電気文化会館': 'Nagoya',
    '犬山': 'Inuyama',
    '守山文化小劇場': 'Nagoya',
    '味岡市民センター': 'Komaki',
    '知多市': 'Chita',
    '春日井': 'Kasugai',
    '長久手': 'Nagakute',
    '多治見': 'Tajimi',
    '幸田町': 'Kota',
    '延岡': 'Nobeoka',
    '新見': 'Niimi',
    '刈谷': 'Kariya',
    '嬉野': 'Matsusaka',
    '豊川': 'Toyokawa',
    '高山': 'Takayama',
    '緑区': 'Nagoya',
    '稲沢': 'Inazawa',
    '可児': 'Kani',
    '神宮東': 'Nagoya',
    'アクトシティ浜松': 'Hamamatsu',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_city(venue):
    for hint, city in CITY_HINTS.items():
        if hint in venue:
            return city
    return None


def fetch_month(year, month):
    response = requests.get(
        CALENDAR_API,
        params={'year': year, 'month': month},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    return response.json().get('calendar', [])


def listing_events():
    # The first surviving calendar records are from 2013. Two future years also
    # cover the orchestra's advance season announcements without a fixed end date.
    months = [
        (year, month)
        for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 3)
        for month in range(1, 13)
    ]
    events = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_month, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                days = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Chubu Philharmonic calendar month',
                    event='crawler_listing_fetch_failed', level='warning',
                    url=f'{CALENDAR_API}?year={year}&month={month}',
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            for day in days:
                item = day.get('event') if isinstance(day, dict) else None
                if item and item.get('id'):
                    events[item['id']] = item
    return list(events.values())


def parse_detail(html, event):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.postArt')
    if not article:
        return None

    title = clean_text(article.select_one('h1.title')).replace('\n', ' ')
    venue_terms = event.get('hall') or []
    venue = clean_text(venue_terms[0].get('name')) if venue_terms else ''
    city = resolve_city(venue)
    raw_date = str(event.get('datetime') or '')[:10]
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return None
    if not all((title, venue, city)):
        return None

    time_from = None
    time_match = re.search(r'([0-2]?\d)\s*[:：]\s*([0-5]\d)\s*開演', clean_text(article.select_one('.start')))
    if time_match and int(time_match.group(1)) < 24:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    series = clean_text(article.select_one('.seriess'))
    if series:
        description_parts.append(series)
    for detail in article.select('.listVox dl.list'):
        label = clean_text(detail.select_one('dt'))
        if not label or 'チケット' in label:
            continue
        body = clean_text(detail.select_one('dd'))
        if body:
            description_parts.append(f'{label}\n{body}')

    return {
        'title': title,
        'date': event_date,
        'url': DETAIL_URL.format(event_id=event['id']),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': '\n\n'.join(description_parts) or None,
    }


def fetch_detail(event):
    url = DETAIL_URL.format(event_id=event['id'])
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, event)


class ChubuPhilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chubu_phil_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        events = listing_events()
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, item): item for item in events}
            for future in as_completed(futures):
                item = futures[future]
                url = DETAIL_URL.format(event_id=item['id'])
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Chubu Philharmonic concert detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    ChubuPhilComCrawler().run()


if __name__ == '__main__':
    main()
