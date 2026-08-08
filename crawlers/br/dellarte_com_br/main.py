import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dellarte.com.br/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Dellarte Soluções Culturais'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, path, params=None):
    response = session.get(f'{API_URL}/{path}', params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def get_collection(session, path, fields=None):
    params = {'per_page': 100}
    if fields:
        params['_fields'] = fields
    return get_json(session, path, params=params)


def get_detail(session, event):
    url = event.get('link') or ''
    if not url:
        return '', None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title_node = soup.select_one('.wrapper-evento .corpo-espetaculo h1')
    title = clean_text(title_node)
    parts = []
    subtitle = clean_text(soup.select_one('.wrapper-evento .subtitulo'))
    about = clean_text(soup.select_one('.wrapper-evento .about'))
    programme = clean_text(soup.select_one('.wrapper-evento .programa'))
    if subtitle:
        parts.append(subtitle)
    if about:
        parts.append(about)
    if programme:
        parts.append(f'Programa\n{programme}')

    if not parts:
        summary = (event.get('yoast_head_json') or {}).get('description')
        if summary:
            parts.append(clean_text(summary))
    return title, clean_text('\n\n'.join(parts)) or None


def parse_session(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    events = get_collection(
        session,
        'eventos',
        'id,link,title,acf,yoast_head_json',
    )
    venues = get_collection(session, 'local', 'id,title,acf')
    cities = get_collection(session, 'cidade', 'id,name')
    city_names = {city['id']: clean_text(city.get('name')) for city in cities}
    venue_data = {}
    for venue in venues:
        acf = venue.get('acf') or {}
        venue_data[venue['id']] = (
            clean_text((venue.get('title') or {}).get('rendered')),
            city_names.get(acf.get('city'), ''),
        )

    details = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_detail, session, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                details[event['id']] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                details[event['id']] = ('', None)

    records = []
    for event in events:
        acf = event.get('acf') or {}
        venue, city = venue_data.get(acf.get('venue'), ('', ''))
        page_title, description = details.get(event['id'], ('', None))
        title = page_title or clean_text((event.get('title') or {}).get('rendered'))
        url = event.get('link') or ''
        if not title or not url or not venue or not city:
            continue
        for session_data in acf.get('sessions') or []:
            parsed = parse_session(session_data.get('session_date'))
            if not parsed:
                continue
            event_date, time_from = parsed
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'BR',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class DellarteComBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dellarte_com_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
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
    DellarteComBrCrawler().run()


if __name__ == '__main__':
    main()
