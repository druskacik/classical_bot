import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.staatstheater-wiesbaden.de/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'spielplan/kalender/')
SCHEDULE_API = urljoin(SOURCE_URL, 'api/schedule')
SOURCE = 'Hessisches Staatstheater Wiesbaden'
CITY = 'Wiesbaden'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = (
        text.replace('\xa0', ' ')
        .replace('\u202f', ' ')
        .replace('\u200b', '')
        .replace('\u00ad', '')
    )
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def schedule_fragments(session):
    """Yield every month exposed by the calendar's scrolling API."""
    page = get_response(session, SCHEDULE_URL)
    soup = BeautifulSoup(page.text, 'html.parser')
    content = soup.select_one('.schedule__content')
    if not content:
        return

    yield str(content)
    dates_to = content.get('data-dates-to')
    final_date = content.get('data-load-forward-until')
    while dates_to and final_date and dates_to < final_date:
        load_from = datetime.strptime(dates_to, '%Y-%m-%d').strftime('%d.%m.%Y')
        payload = get_response(
            session,
            SCHEDULE_API,
            params={'filter': '', 'loadForwardFrom': load_from},
        ).json()
        fragment = payload.get('schedule')
        new_dates_to = payload.get('datesTo')
        if not fragment or not new_dates_to or new_dates_to <= dates_to:
            break
        yield fragment
        dates_to = new_dates_to


def listing_record(performance):
    title_node = performance.select_one('.performance__title [itemprop="name"]')
    link = performance.select_one('.performance__title a[href]')
    start_node = performance.select_one('meta[itemprop="startDate"][content]')
    stage_node = performance.select_one('.performance__stage')
    if not all((title_node, link, start_node, stage_node)):
        return None

    title = clean_text(title_node.get_text(' ', strip=True)).rstrip(':').strip()
    venue = clean_text(stage_node.get_text(' ', strip=True)).rstrip(':').strip()
    start_value = (start_node.get('content') or '').strip()
    try:
        start = datetime.fromisoformat(start_value)
    except ValueError:
        return None

    # All stages in this institution's published calendar are Wiesbaden
    # venues. Avoid accepting an explicitly named touring location should one
    # appear in a future season.
    if not title or not venue or re.search(r'\b(gastspiel|tournee)\b', venue, re.I):
        return None

    description_parts = []
    for selector in (
        '.performance__subtitle',
        '.performance__authorcomposer',
        '.performance__productioninfo',
        '.performance__infotext',
    ):
        for node in performance.select(selector):
            value = clean_text(node.get_text(' ', strip=True))
            if value and value not in description_parts:
                description_parts.append(value)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, link.get('href')),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': '\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, record):
    soup = BeautifulSoup(get_response(session, record['url']).text, 'html.parser')
    parts = []
    summary = record.get('description')
    if summary:
        parts.append(summary)

    # Content accordions contain the synopsis, programme notes, and other
    # production text. Termine is excluded because it merely repeats dates,
    # ticket prices, and cast controls from the calendar.
    for accordion in soup.select('.accordionlarge'):
        heading = clean_text(
            accordion.select_one('.accordionlarge__title').get_text(' ', strip=True)
        ) if accordion.select_one('.accordionlarge__title') else ''
        if heading.casefold().startswith(('termine', 'besetzung')):
            continue
        body_parts = [
            clean_text(node.get_text('\n', strip=True))
            for node in accordion.select('.accordionlarge__body .richtext')
        ]
        body_parts = [value for value in body_parts if value]
        if body_parts:
            value = (heading + '\n' if heading else '') + '\n\n'.join(body_parts)
            if value not in parts:
                parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records_by_url = {}
    for fragment in schedule_fragments(session):
        soup = BeautifulSoup(fragment, 'html.parser')
        for performance in soup.select('.performance'):
            record = listing_record(performance)
            if record:
                records_by_url[record['url']] = record

    records = list(records_by_url.values())
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(detail_description, session, record): record
            for record in records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class StaatstheaterWiesbadenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='staatstheater_wiesbaden_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    StaatstheaterWiesbadenDeCrawler().run()


if __name__ == '__main__':
    main()
