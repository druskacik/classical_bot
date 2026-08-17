import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.apollomusicfestival.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Apollo Music Festival'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def parse_date_time(value, year):
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*'
        r'([A-Za-z]+\s+\d{1,2})\s*\|\s*'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
        clean_text(value),
        re.I,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {year}', '%B %d %Y'
        ).date().isoformat()
        normalized_time = re.sub(r'\s+', '', match.group(2)).upper()
        time_format = '%I:%M%p' if ':' in normalized_time else '%I%p'
        time_from = datetime.strptime(normalized_time, time_format).strftime('%H:%M')
        return event_date, time_from
    except ValueError:
        return None, None


def detail_description(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return None

    # Programme and performer blocks follow the first separator.  This avoids
    # repeating the title, date and address while retaining work/composer text.
    separator = main.find('hr')
    parts = []
    if separator:
        for node in separator.find_all_next(['h2', 'h3', 'h4', 'p']):
            if not main.find(lambda tag: tag is node):
                break
            text = clean_text(node.get_text(' ', strip=True))
            if text and text not in parts:
                parts.append(text)
    return '\n'.join(parts) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    heading_text = clean_text(soup.select_one('main').get_text(' ', strip=True))
    year_match = re.search(r'\b(20\d{2})\b', heading_text)
    if not year_match:
        raise ValueError('Could not determine the festival calendar year')
    year = int(year_match.group(1))

    blocks = soup.select('main .sqs-block')
    descriptions = {}
    records = []
    for index, block in enumerate(blocks):
        title_node = next(
            (node for node in block.find_all('h3') if clean_text(node.get_text(' ', strip=True))),
            None,
        )
        if not title_node:
            continue
        title = clean_text(title_node.get_text(' ', strip=True))
        paragraphs = [clean_text(p.get_text(' ', strip=True)) for p in block.find_all('p')]
        paragraphs = [value for value in paragraphs if value]
        date_index = next(
            (i for i, value in enumerate(paragraphs) if '|' in value), None
        )
        if not title or date_index is None or len(paragraphs) <= date_index + 3:
            continue
        event_date, time_from = parse_date_time(paragraphs[date_index], year)
        venue = paragraphs[date_index + 1]
        city_line = paragraphs[date_index + 3]
        city_match = re.match(r'(.+?),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$', city_line)

        detail_url = None
        for following in blocks[index + 1:]:
            if any(clean_text(node.get_text(' ', strip=True)) for node in following.find_all('h3')):
                break
            link = following.find('a', href=True)
            if link:
                detail_url = urljoin(EVENTS_URL, link['href'])
                break
        if not event_date or not venue or not city_match or not detail_url:
            log_message(
                'Skipping calendar entry with incomplete required fields',
                event='crawler_record_skipped',
                level='warning',
                url=detail_url or EVENTS_URL,
                title=title or None,
            )
            continue

        try:
            if detail_url not in descriptions:
                descriptions[detail_url] = detail_description(session, detail_url)
            description = descriptions[detail_url]
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=detail_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            description = None

        # A bare storytime listing provides no evidence of substantial live
        # performance.  Other calendar entries publish repertoire/performers.
        if not description:
            log_message(
                'Skipping entry without published performance details',
                event='crawler_record_skipped',
                level='warning',
                url=detail_url,
                title=title,
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': detail_url,
            'time_from': time_from,
            'venue': venue,
            'city': city_match.group(1).strip(),
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class ApolloMusicFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='apollomusicfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    ApolloMusicFestivalComCrawler().run()


if __name__ == '__main__':
    main()
