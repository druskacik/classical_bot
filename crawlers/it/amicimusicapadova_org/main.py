import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.amicimusicapadova.org/'
SOURCE = 'Amici della Musica di Padova'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Referer': f'{SOURCE_URL}calendario/',
}

API_PARAMS = {
    'per_page': 50,
    'start_date': '1900-01-01 00:00:00',
    'end_date': '2100-12-31 23:59:59',
    'status': 'publish',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(value), 'html.parser')
    for element in soup.select('script, style'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_html(event.get('title'))
    url = event.get('url') or event.get('website')
    venue_data = event.get('venue') or {}
    venue = clean_html(venue_data.get('venue'))
    # Older local venue records often omit the city while retaining a Padova
    # street or institution in the address. Touring venues carry their own city.
    city = clean_html(venue_data.get('city')) or 'Padova'

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city:
        return None

    description_parts = []
    for field in ('excerpt', 'description'):
        text = clean_html(event.get(field))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AmiciMusicaPadovaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicimusicapadova_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount(
            'https://',
            HTTPAdapter(max_retries=Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            )),
        )

        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            try:
                response = session.get(
                    API_URL,
                    params={**API_PARAMS, 'page': page},
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Amici della Musica di Padova events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            total_pages = int(payload.get('total_pages') or 1)
            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped event with incomplete required fields',
                        event='crawler_record_skipped',
                        level='warning',
                        url=event.get('url') or API_URL,
                        event_id=event.get('id'),
                    )
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    AmiciMusicaPadovaOrgCrawler().run()


if __name__ == '__main__':
    main()
