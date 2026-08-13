import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.t-bunka.jp/'
STAGE_URL = urljoin(SOURCE_URL, 'stage/')
AJAX_URL = urljoin(SOURCE_URL, 'cms/wp-admin/admin-ajax.php')
SOURCE = 'Tokyo Bunka Kaikan'
CITY = 'Tokyo'
ARCHIVE_START = '2025-01-01'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url, *, data=None):
    response = session.post(url, data=data, timeout=60) if data else session.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def listing_pages(session):
    form = {'schedule_stage_date': ARCHIVE_START}
    html = get_html(session, STAGE_URL, data=form)
    offset = 0

    for page_number in range(500):
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.select('#result > tr') or soup.find_all('tr', recursive=False)
        if not rows:
            return
        yield rows

        date_count = sum(1 for row in rows if row.select_one('.date_row'))
        offset += date_count
        if not date_count or rows[-1].get('data-is_last_more') == '1':
            return

        payload = {
            'action': 'more_stage_list',
            'lang': 'ja',
            'offset': str(offset),
            **form,
        }
        html = get_html(session, AJAX_URL, data=payload)

    log_message(
        'Stopped calendar pagination at safety limit',
        event='crawler_pagination_limit',
        level='warning',
        url=STAGE_URL,
        page_count=500,
    )


def field_from_listing(link, alt):
    for item in link.select('li'):
        icon = item.find('img')
        if icon and clean_text(icon.get('alt')) == alt:
            return clean_text(item)
    return ''


def parse_listing(session):
    events = []
    current_date = None
    for rows in listing_pages(session):
        for row in rows:
            date_cell = row.select_one('.date_row')
            if date_cell:
                match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', clean_text(date_cell))
                if not match:
                    current_date = None
                    continue
                try:
                    current_date = date(*map(int, match.groups())).isoformat()
                except ValueError:
                    current_date = None

            link = row.select_one('td > a[href*="/stage/"]')
            title_node = link.select_one('h2') if link else None
            if not current_date or not link or not title_node:
                continue
            venue = field_from_listing(link, '会場')
            venue = re.sub(r'[（(]アクセス[）)]', '', venue)
            venue = venue.replace('※会場は東京文化会館ではございません。', '').strip()
            if not venue:
                continue
            events.append({
                'title': clean_text(title_node),
                'date': current_date,
                'url': urljoin(SOURCE_URL, link.get('href')),
                'time_text': field_from_listing(link, '日程'),
                'venue': venue,
            })
    return events


def detail_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    contents = soup.select_one('#contents')
    if not contents:
        return None

    parts = []
    heading = contents.select_one('.center')
    intro = heading.find_next_sibling('p') if heading else None
    intro_text = clean_text(intro)
    if intro_text:
        parts.append(intro_text)

    useful_labels = ('出演', '曲目', 'プログラム', '公演内容', '内容')
    for row in contents.select('table.t_stage tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) < 2:
            continue
        label = clean_text(cells[0])
        value = clean_text(cells[1])
        if value and any(term in label for term in useful_labels):
            parts.append(f'{label}\n{value}')

    description = '\n\n'.join(dict.fromkeys(parts))
    return description or None


def start_times(value):
    # Opening and reception times are also shown in the same string. A
    # performance time is identified by the site's start/range notation.
    times = re.findall(
        r'(?<!\d)([0-2]?\d):([0-5]\d)(?=\s*(?:開演|審査開始|～|〜))',
        value or '',
    )
    return list(dict.fromkeys(f'{int(hour):02d}:{minute}' for hour, minute in times)) or [None]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = parse_listing(session)
    descriptions = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_html, session, url): url
            for url in dict.fromkeys(event['url'] for event in events)
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = detail_description(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None

    records = []
    for event in events:
        for time_from in start_times(event['time_text']):
            records.append({
                'title': event['title'],
                'date': event['date'],
                'url': event['url'],
                'time_from': time_from,
                'venue': event['venue'],
                'city': CITY,
                'country_code': 'JP',
                'description': descriptions.get(event['url']),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue'], item['url']
    ))


class TBunkaJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='t_bunka_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TBunkaJpCrawler().run()


if __name__ == '__main__':
    main()
