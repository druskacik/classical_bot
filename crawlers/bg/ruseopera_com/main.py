import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ruseopera.com/'
PROGRAM_URL = f'{SOURCE_URL}category/program'
SOURCE = 'State Opera Ruse'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'bg-BG,bg;q=0.9,en;q=0.7',
}

MONTHS = {
    'яну': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'май': 5, 'юни': 6,
    'юли': 7, 'авг': 8, 'сеп': 9, 'окт': 10, 'ное': 11, 'дек': 12,
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def upcoming_date(day_text, month_text, today=None):
    """Resolve the year omitted by the site's explicitly upcoming calendar."""
    today = today or date.today()
    month = MONTHS.get(clean_text(month_text).lower().rstrip('.'))
    try:
        day = int(clean_text(day_text))
    except (TypeError, ValueError):
        return None

    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate.isoformat()
    return None


def parse_item(item, today=None):
    link = item.select_one('a[href*="/productions/"]')
    title = clean_text(item.select_one('.event_entase_title'))
    venue = clean_text(item.select_one('.event_entase_location_placeName'))
    city = clean_text(item.select_one('.event_entase_location_cityName'))
    date_node = item.select_one('.event_entase_dateonly')
    month_node = date_node.select_one('span') if date_node else None
    day_text = date_node.find(string=True, recursive=False) if date_node else ''
    event_date = upcoming_date(day_text, month_node, today=today)
    url = link.get('href', '').strip() if link else ''
    if not all((title, event_date, url, venue, city)):
        return None

    time_match = re.search(
        r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b',
        clean_text(item.select_one('.event_entase_timeonly')),
    )
    author = clean_text(item.select_one('.event_meta_author'))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'BG',
        'description': author or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, record):
    response = session.get(record['url'], timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []
    if record.get('description'):
        parts.append(record['description'])
    for element in soup.select('.elementor-widget-text-editor'):
        text = clean_text(element)
        # Exclude the repeated footer's ticket-office opening hours.
        if len(text) >= 100 and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


class RuseoperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ruseopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BG',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(PROGRAM_URL, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for item in soup.select('.event_items .event_item'):
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                link = item.select_one('a[href*="/productions/"]')
                log_message(
                    'Skipped incomplete State Opera Ruse event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=link.get('href', '') if link else PROGRAM_URL,
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, or city is missing',
                )

        with ThreadPoolExecutor(max_workers=8) as executor:
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
                        'Failed to scrape State Opera Ruse production detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    RuseoperaComCrawler().run()


if __name__ == '__main__':
    main()
