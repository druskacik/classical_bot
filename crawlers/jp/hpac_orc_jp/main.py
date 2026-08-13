import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hpac-orc.jp/'
SOURCE = 'Hyogo Performing Arts Center Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

PLACE_CITIES = {
    13: 'Nishinomiya', 14: 'Nishinomiya', 19: 'Nishinomiya',
    21: 'Akashi', 22: 'Tamba-Sasayama', 24: 'Hatsukaichi', 25: 'Yabu',
    26: 'Ako', 27: 'Kanazawa', 28: 'Kobe', 29: 'Kurume',
    36: 'Takasago', 37: 'Taishi', 38: 'Sumoto', 39: 'Kitakyushu',
    40: 'Ofunato', 41: 'Misawa', 43: 'Osaka', 44: 'Kawasaki',
    45: 'Taka', 46: 'Kato',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = unescape(text).replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def labelled_value(soup, label):
    for row in soup.select('.infoTblList'):
        heading = row.select_one('.infoTblListTitle')
        if clean_text(heading) == label:
            return row.select_one('.infoTblListContent')
    return None


def parse_occurrences(raw_value):
    # Some pages list two performances, with the second date abbreviated to
    # just its day.  Carrying the last explicit year/month handles that layout.
    token_re = re.compile(
        r'(?:(?P<year>20\d{2})年\s*(?P<month>\d{1,2})月\s*)?'
        r'(?P<day>\d{1,2})日(?:\([^)]*\))?'
        r'(?P<tail>.*?)(?=(?:20\d{2}年\s*\d{1,2}月\s*)?\d{1,2}日|$)'
    )
    occurrences = []
    year = month = None
    for match in token_re.finditer(raw_value):
        if match.group('year'):
            year, month = int(match.group('year')), int(match.group('month'))
        if year is None or month is None:
            continue
        try:
            event_date = date(year, month, int(match.group('day'))).isoformat()
        except ValueError:
            continue
        time_match = re.search(r'開演\s*([0-2]?\d)\s*[:：]\s*([0-5]\d)', match.group('tail'))
        time_from = None
        if time_match and int(time_match.group(1)) < 24:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        occurrences.append((event_date, time_from))
    return occurrences


def parse_detail(html, url, place_ids, place_names):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.concertSchedule.detail h1')).replace('\n', ' ')
    datetime_node = labelled_value(soup, '日時')
    raw_datetime = clean_text(datetime_node).replace('\n', ' ')

    venue_node = labelled_value(soup, '会場')
    venue = clean_text(venue_node).replace('\n', ' ')
    if not venue and venue_node:
        image = venue_node.select_one('img[alt]')
        venue = clean_text(image.get('alt')) if image else ''
    place_id = next((item for item in place_ids if item in place_names), None)
    if not venue and place_id is not None:
        venue = place_names[place_id]
    city = PLACE_CITIES.get(place_id)

    occurrences = parse_occurrences(raw_datetime)
    if not all((title, venue, city, occurrences)):
        return []

    description_parts = []
    content = soup.select_one('.concertSchedule.detail .sBody')
    if content:
        for node in content.select('script, style, .ticketBtn, .ticketInfo, .priceBlock'):
            node.decompose()
        description_parts.append(clean_text(content))
    description = '\n\n'.join(part for part in description_parts if part) or None

    return [{
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
    } for event_date, time_from in occurrences]


def api_collection(session, endpoint, fields=None):
    records = []
    page = 1
    while True:
        params = {'per_page': 100, 'page': page}
        if fields:
            params['_fields'] = fields
        response = session.get(f'{API_URL}/{endpoint}', params=params, timeout=60)
        response.raise_for_status()
        records.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            break
        page += 1
    return records


def fetch_detail(session, post, place_names):
    url = post['link']
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return parse_detail(response.text, url, post.get('concert_place', []), place_names)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    places = api_collection(session, 'concert_place', 'id,name')
    place_names = {item['id']: clean_text(item['name']) for item in places}
    posts = api_collection(session, 'concert', 'id,link,concert_place')

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_detail, session, post, place_names): post['link']
            for post in posts
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape HPAC Orchestra concert detail',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class HpacOrcJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hpac_orc_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HpacOrcJpCrawler().run()


if __name__ == '__main__':
    main()
