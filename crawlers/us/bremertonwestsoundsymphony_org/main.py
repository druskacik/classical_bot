import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bremertonwestsoundsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'events-calendar')
SOURCE = 'Bremerton WestSound Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4}),\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)$',
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(r',\s*([^,]+?),?\s+WA\s+\d{5}(?:-\d{4})?$', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = DATE_RE.fullmatch(clean_text(value))
    if not match:
        return None
    month, day, year, event_time = match.groups()
    try:
        event_date = datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
        parsed_time = datetime.strptime(event_time.replace(' ', '').upper(), '%I:%M%p')
    except ValueError:
        try:
            parsed_time = datetime.strptime(event_time.replace(' ', '').upper(), '%I%p')
        except ValueError:
            return None
    return event_date, parsed_time.strftime('%H:%M')


def detail_description(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        main = soup.find('main')
        return clean_text(main.get_text('\n', strip=True)) if main else None
    except requests.RequestException as error:
        log_message(
            'Could not fetch concert detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.find('main')
    if not main:
        return []

    detail_urls = [
        urljoin(LISTING_URL, link['href'])
        for link in main.find_all('a', href=True)
        if clean_text(link.get_text(' ', strip=True)).lower().startswith('read more')
    ]
    blocks = clean_text(main.get_text('\n', strip=True)).split('Read More...')
    records = []

    for block, url in zip(blocks, detail_urls):
        lines = [line for line in clean_text(block).splitlines() if line]
        date_indexes = [index for index, line in enumerate(lines) if parse_date_time(line)]
        if not date_indexes:
            continue

        last_date_index = date_indexes[-1]
        venue = lines[last_date_index + 1] if last_date_index + 1 < len(lines) else ''
        address = lines[last_date_index + 2] if last_date_index + 2 < len(lines) else ''
        city_match = ADDRESS_RE.search(address)
        if not venue or not city_match:
            continue
        city = clean_text(city_match.group(1))
        description = detail_description(session, url)

        base_title = lines[date_indexes[0] - 1] if date_indexes[0] else ''
        for position, date_index in enumerate(date_indexes):
            parsed = parse_date_time(lines[date_index])
            title = base_title
            if position:
                title_parts = lines[date_indexes[position - 1] + 1:date_index]
                if title_parts:
                    title = ' '.join(title_parts)
            if not parsed or not title:
                continue
            event_date, time_from = parsed
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No concerts found on calendar',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BremertonWestSoundSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bremertonwestsoundsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BremertonWestSoundSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
