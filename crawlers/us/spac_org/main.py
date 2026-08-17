import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://spac.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap-posttype-event.xml'
SOURCE = 'Saratoga Performing Arts Center'
CITY = 'Saratoga Springs'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)'
    r'\s*[•|]\s*'
    r'(?P<month>[A-Z][a-z]{2,8})\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})'
    r'\s*[•|]\s*(?P<time>\d{1,2}:\d{2}\s*[ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    return [node.get_text(strip=True) for node in soup.select('url > loc')]


def parse_occurrences(value):
    occurrences = []
    for match in DATE_PATTERN.finditer(value):
        raw_date = f'{match.group("month")} {match.group("day")}, {match.group("year")}'
        try:
            event_date = datetime.strptime(raw_date, '%b %d, %Y').date()
        except ValueError:
            try:
                event_date = datetime.strptime(raw_date, '%B %d, %Y').date()
            except ValueError:
                continue
        event_time = datetime.strptime(
            re.sub(r'\s+', '', match.group('time')).upper(), '%I:%M%p'
        ).strftime('%H:%M')
        occurrences.append((event_date.isoformat(), event_time))
    return occurrences


def event_description(soup):
    parts = []
    for block in soup.select('main .c-container .c-col-text-area.c-wysiwyg'):
        # Membership and ticket calls-to-action add no useful programme evidence.
        if block.select_one('.c-event-cta'):
            continue
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('.c-event-masthead__title')
    date_node = soup.select_one('.c-masthead__daterange')
    venue_node = soup.select_one('.c-masthead__venue p')

    title = clean_text(title_node)
    venue = clean_text(venue_node)
    occurrences = parse_occurrences(clean_text(date_node))
    if not title or not venue or not occurrences:
        return []

    description = event_description(soup)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, event_time in occurrences
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(future.result().text, url))
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
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class SpacOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='spac_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
    SpacOrgCrawler().run()


if __name__ == '__main__':
    main()
