import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.npac-ntt.org/index'
CALENDAR_URL = 'https://www.npac-ntt.org/pgcalendar'
SOURCE = '臺中國家歌劇院 National Taichung Theater'
CITY = 'Taichung'
COUNTRY_CODE = 'TW'
FIRST_ARCHIVE_MONTH = (2016, 9)
FUTURE_MONTHS = 18
INCLUDED_TYPES = {'音樂', '舞蹈', '歌劇', '音樂劇', '親子'}
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_offset(year, month, offset):
    number = year * 12 + month - 1 + offset
    return number // 12, number % 12 + 1


def calendar_months():
    today = date.today()
    last = month_offset(today.year, today.month, FUTURE_MONTHS)
    current = FIRST_ARCHIVE_MONTH
    while current <= last:
        yield current
        current = month_offset(*current, 1)


def fetch_html(url):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(attempt + 1)
    raise last_error


def listing_urls(year, month):
    url = f'{CALENDAR_URL}/query{year:04d}{month:02d}'
    soup = fetch_html(url)
    return {
        urljoin(SOURCE_URL, link['href'])
        for cell in soup.select('td:not(.notCurrMonth)')
        for link in cell.select('a.event-link[href*="/program/events/"]')
    }


def info_fields(soup):
    fields = {}
    for item in soup.select('.event-infolist .info-item'):
        label = clean_text(item.select_one('.info-title'))
        content = item.select_one('.info-cont')
        if label and content:
            fields[label] = clean_text(content)
    return fields


def description_from(soup):
    parts = []
    for selector in ('#SortIntroduction', '#SortTrack', '#SortPerson', '#SortCreativeTeam'):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def detail_records(url):
    soup = fetch_html(url)
    fields = info_fields(soup)
    event_type = fields.get('節目類型', '')
    if event_type not in INCLUDED_TYPES:
        return []

    title = clean_text(soup.select_one('h1.post-title')) or fields.get('標題', '')
    venue = fields.get('演出場地', '')
    if not title or not venue or venue == '其他場地':
        return []

    description = description_from(soup)
    records = []
    seen = set()
    for span in soup.select('.event-infolist .info-item .event-time span'):
        text = clean_text(span).replace('\n', ' ')
        match = re.search(r'(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})', text)
        if not match:
            continue
        try:
            event_date = date(*map(int, match.groups()[:3])).isoformat()
        except ValueError:
            continue
        time_from = f'{match.group(4)}:{match.group(5)}'
        occurrence = (event_date, time_from)
        if occurrence in seen:
            continue
        seen.add(occurrence)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    event_urls = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(listing_urls, year, month): f'{year:04d}-{month:02d}'
            for year, month in calendar_months()
        }
        for future in as_completed(futures):
            month = futures[future]
            try:
                event_urls.update(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_URL}/query{month.replace("-", "")}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_records, url): url for url in event_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'], record['title'], record['url']),
    )


class NpacNttOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='npac_ntt_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    NpacNttOrgCrawler().run()


if __name__ == '__main__':
    main()
