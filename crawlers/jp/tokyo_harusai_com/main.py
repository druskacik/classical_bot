import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tokyo-harusai.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/program_info'
SOURCE = 'Spring Festival in Tokyo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.7',
}


def make_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def clean_text(element):
    if not element:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_and_times(value):
    match = re.search(
        r'(20\d{2})(?:年|/)\s*(\d{1,2})(?:月|/)\s*(\d{1,2})(?:日)?', value
    )
    if not match:
        return None, []
    try:
        event_date = date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None, []

    # Pages occasionally advertise two performances in the same date line.
    times = []
    for hour, minute in re.findall(r'(?<!\d)([0-2]?\d):([0-5]\d)(?=\s*(?:開演|[／/,・]|$|\())', value):
        parsed = f'{int(hour):02d}:{minute}'
        if parsed not in times:
            times.append(parsed)
    if not times:
        opening = re.search(r'([0-2]?\d):([0-5]\d)\s*開演', value)
        if opening:
            times.append(f'{int(opening.group(1)):02d}:{opening.group(2)}')
    return event_date, times or [None]


def city_for_venue(venue):
    if '川崎' in venue:
        return 'Kawasaki'
    if '横浜' in venue:
        return 'Yokohama'
    if '千葉' in venue:
        return 'Chiba'
    if 'さいたま' in venue or '埼玉' in venue:
        return 'Saitama'
    return 'Tokyo'


def event_details(soup):
    detail = soup.select_one('.detail_infomation .detail_cont.jp')
    if not detail:
        return [], ''

    occurrence_text = ''
    venue = ''
    heading = detail.find(['h5', 'h6'], string=lambda value: value and '日時・会場' in value)
    if heading:
        paragraphs = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ('h5', 'h6'):
                break
            text = clean_text(sibling)
            if text:
                paragraphs.append(text)
        if paragraphs:
            occurrence_text = '\n'.join(paragraphs)
            lines = [line for line in occurrence_text.splitlines() if line.strip()]
            venue_lines = [line for line in lines if not re.search(r'20\d{2}年', line)]
            if venue_lines:
                venue = venue_lines[-1].strip()
    else:
        occurrence_text = clean_text(detail)
        lines = [line for line in occurrence_text.splitlines() if line.strip()]
        if len(lines) > 1:
            venue = lines[-1].strip()

    date_matches = list(re.finditer(
        r'20\d{2}(?:年|/)\s*\d{1,2}(?:月|/)\s*\d{1,2}(?:日)?', occurrence_text
    ))
    occurrences = []
    for index, match in enumerate(date_matches):
        end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(occurrence_text)
        event_date, times = parse_date_and_times(occurrence_text[match.start():end])
        if event_date:
            occurrences.extend((event_date, time_from) for time_from in times)
    return occurrences, venue


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.title_main.jp'))
    occurrences, venue = event_details(soup)
    description_parts = [
        clean_text(element)
        for element in soup.select('.detail_infomation .detail_cont.jp')
        if clean_text(element)
    ]
    description = '\n\n'.join(description_parts) or None
    if not title or not occurrences or not venue:
        return []

    base_record = {
        'title': title,
        'url': url,
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'JP',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    return [
        {**base_record, 'date': event_date, 'time_from': time_from}
        for event_date, time_from in occurrences
    ]


def fetch_event(url):
    response = make_session().get(url, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class TokyoHarusaiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tokyo_harusai_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def event_urls(self):
        session = make_session()
        urls = []
        page = 1
        while True:
            response = session.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'id',
                    'order': 'asc',
                    '_fields': 'link',
                },
                timeout=45,
            )
            response.raise_for_status()
            items = response.json()
            urls.extend(item['link'] for item in items if item.get('link'))
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1
        return list(dict.fromkeys(urls))

    def scrape(self):
        records = []
        urls = self.event_urls()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    parsed = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Spring Festival in Tokyo concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if not parsed:
                    log_message(
                        'Skipped incomplete Spring Festival in Tokyo concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or city is missing',
                    )
                    continue
                records.extend(parsed)
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    TokyoHarusaiComCrawler().run()


if __name__ == '__main__':
    main()
