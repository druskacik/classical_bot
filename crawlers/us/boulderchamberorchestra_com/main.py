import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.boulderchamberorchestra.com/'
SOURCE = 'Boulder Chamber Orchestra'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\b'
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9]):([0-5]\d)\b', re.IGNORECASE)
MERIDIEM_RE = re.compile(r'\b([AP]M)\b', re.IGNORECASE)
CITY_RE = re.compile(r'^(.+?),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?$')


def clean_lines(element):
    if element is None:
        return []
    return [
        re.sub(r'\s+', ' ', line).strip()
        for line in element.get_text('\n', strip=True).splitlines()
        if re.sub(r'\s+', ' ', line).strip()
    ]


def parse_time(value):
    match = TIME_RE.search(value)
    meridiem = MERIDIEM_RE.search(value)
    if not match or not meridiem:
        return None
    return datetime.strptime(
        f'{match.group(1)}:{match.group(2)} {meridiem.group(1).upper()}', '%I:%M %p'
    ).strftime('%H:%M')


def value_after(lines, label):
    try:
        index = next(i for i, value in enumerate(lines) if value.casefold() == label.casefold())
    except StopIteration:
        return None
    return lines[index + 1] if index + 1 < len(lines) else None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    lines = clean_lines(main)
    title_element = main.select_one('h1, h2, h3') if main else None
    title_lines = clean_lines(title_element)
    if not title_lines:
        return None

    date_match = next((DATE_RE.search(line) for line in lines if DATE_RE.search(line)), None)
    venue = value_after(lines, 'Where')
    if not date_match or not venue:
        return None

    try:
        event_date = datetime.strptime(date_match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None

    date_index = next(i for i, line in enumerate(lines) if DATE_RE.search(line))
    time_from = next(
        (parse_time(line) for line in lines[date_index + 1:date_index + 4] if parse_time(line)),
        None,
    )

    try:
        where_index = next(i for i, value in enumerate(lines) if value.casefold() == 'where')
    except StopIteration:
        return None
    city = None
    for line in lines[where_index + 2:where_index + 6]:
        city_match = CITY_RE.match(line)
        if city_match:
            city = city_match.group(1).strip()
            break
    if not city:
        return None

    description = None
    try:
        program_index = next(i for i, value in enumerate(lines) if value.casefold() == 'program')
        program_lines = lines[program_index + 1:]
        while program_lines and program_lines[-1].casefold() in {
            'explore digital program', 'explore digital program →'
        }:
            program_lines.pop()
        description = '\n'.join(program_lines) or None
    except StopIteration:
        pass

    return {
        'title': ' — '.join(title_lines),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': description,
    }


class BoulderChamberOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='boulderchamberorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Boulder Chamber Orchestra sitemap',
                event='crawler_fetch_failed', level='error', url=SITEMAP_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(response.content, 'xml')
        urls = sorted({
            loc.get_text(strip=True) for loc in sitemap.find_all('loc')
            if urlparse(loc.get_text(strip=True)).netloc == urlparse(SOURCE_URL).netloc
            and '/program-notes' not in loc.get_text(strip=True)
        })

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    page = future.result()
                    page.raise_for_status()
                    record = parse_event(page.text, url)
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Boulder Chamber Orchestra page',
                        event='crawler_page_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BoulderChamberOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
