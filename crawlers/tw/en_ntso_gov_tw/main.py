import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as calendar_date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://en.ntso.gov.tw/home/en-us'
SOURCE = 'National Taiwan Symphony Orchestra'
API_URL = 'https://themedata.culture.tw/api'
PAGE_SIZE = 100
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://en.ntso.gov.tw/',
}

# The API supplies a Taiwanese postal code for each performance. Venue hints
# cover the historical records whose postal code was missing or malformed.
POSTCODE_CITIES = {
    '100': 'Taipei', '103': 'Taipei', '104': 'Taipei', '105': 'Taipei',
    '106': 'Taipei', '108': 'Taipei', '110': 'Taipei', '111': 'Taipei',
    '112': 'Taipei', '114': 'Taipei', '115': 'Taipei', '116': 'Taipei',
    '200': 'Keelung', '300': 'Hsinchu', '302': 'Zhubei', '320': 'Taoyuan',
    '330': 'Taoyuan', '350': 'Zhunan', '360': 'Miaoli', '400': 'Taichung',
    '401': 'Taichung', '403': 'Taichung', '404': 'Taichung', '406': 'Taichung',
    '407': 'Taichung', '408': 'Taichung', '413': 'Taichung', '500': 'Changhua',
    '510': 'Yuanlin', '600': 'Chiayi', '640': 'Douliu', '700': 'Tainan',
    '701': 'Tainan', '702': 'Tainan', '708': 'Tainan', '800': 'Kaohsiung',
    '801': 'Kaohsiung', '802': 'Kaohsiung', '803': 'Kaohsiung',
    '804': 'Kaohsiung', '806': 'Kaohsiung', '807': 'Kaohsiung',
    '811': 'Kaohsiung', '830': 'Kaohsiung', '900': 'Pingtung',
    '970': 'Hualien',
}
VENUE_CITIES = (
    ('Kaohsiung', 'Kaohsiung'), ('Weiwuying', 'Kaohsiung'),
    ('Taichung', 'Taichung'), ('Taipei', 'Taipei'),
    ('National Concert Hall', 'Taipei'), ('Hualien', 'Hualien'),
    ('Tainan', 'Tainan'), ('Chiayi', 'Chiayi'), ('Hsinchu', 'Hsinchu'),
    ('Miaoli', 'Miaoli'), ('Yuanlin', 'Yuanlin'), ('Yunlin', 'Douliu'),
    ('Keelung', 'Keelung'), ('Pingtung', 'Pingtung'),
    ('Zhongli', 'Taoyuan'), ('NTSO Concert Hall', 'Taichung'),
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(f'{API_URL}/site/config', timeout=30)
    response.raise_for_status()
    for site in response.json():
        for language in site.get('langs') or []:
            if language.get('lang') == 'en-us' and language.get('siteToken'):
                session.headers['access_token'] = language['siteToken']
                return session
    raise ValueError('English NTSO API token was not present in site config')


def fetch_catalogue(session):
    rows = []
    offset = 0
    while True:
        response = session.get(
            f'{API_URL}/cms/Ticket',
            params={
                'limit': PAGE_SIZE,
                'offset': offset,
                'query': '{}',
                'sort': 'startDate',
                'order': 'asc',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get('rows') or []
        rows.extend(page)
        offset += len(page)
        if not page or offset >= int(payload.get('total') or 0):
            break
    return rows


def fetch_detail(session, event_id):
    response = session.get(f'{API_URL}/cms/Ticket/{event_id}', timeout=30)
    response.raise_for_status()
    return response.json()


def resolve_city(performance):
    postcode = re.sub(r'\D', '', str(performance.get('postCode') or ''))[:3]
    if postcode in POSTCODE_CITIES:
        return POSTCODE_CITIES[postcode]
    venue = clean_text(performance.get('perfName'))
    for marker, city in VENUE_CITIES:
        if marker.casefold() in venue.casefold():
            return city
    return None


def detail_records(detail):
    title = clean_text(detail.get('name'))
    event_id = detail.get('id')
    if not title or not event_id:
        return []
    url = f'{SOURCE_URL}/Ticket/{event_id}'
    description = clean_text(detail.get('content')) or clean_text(detail.get('synopsis')) or None
    records = []
    for performance in detail.get('performances') or []:
        date = clean_text(performance.get('perfStartDate'))
        venue = clean_text(performance.get('perfName'))
        city = resolve_city(performance)
        try:
            calendar_date.fromisoformat(date)
        except ValueError:
            continue
        if not venue or not city:
            continue
        time_from = clean_text(performance.get('perfStartTime'))[:5] or None
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class EnNtsoGovTwCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='en_ntso_gov_tw',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='TW',
        upload_target='classical',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city', 'description'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = get_session()
        try:
            catalogue = fetch_catalogue(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch NTSO concert catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=f'{API_URL}/cms/Ticket',
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_detail, session, row.get('id')): row.get('id')
                for row in catalogue if row.get('id')
            }
            for future in as_completed(futures):
                event_id = futures[future]
                try:
                    records.extend(detail_records(future.result()))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch NTSO concert detail',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=f'{SOURCE_URL}/Ticket/{event_id}',
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    EnNtsoGovTwCrawler().run()


if __name__ == '__main__':
    main()
