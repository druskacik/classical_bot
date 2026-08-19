import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.romesymphony.org/'
SEASON_URL = f'{SOURCE_URL}upcoming-shows'
SOURCE = 'Rome Symphony Orchestra'
CITY = 'Rome'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    normalized = re.sub(r'\bSept\.', 'Sep.', value, flags=re.IGNORECASE)
    match = re.search(r'([A-Z][a-z]+)\.?,?\s+(\d{1,2}),\s+(20\d{2})', normalized)
    if not match:
        return None
    for date_format in ('%b %d %Y', '%B %d %Y'):
        try:
            return datetime.strptime(' '.join(match.groups()), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([ap])\.?m\.?', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_item(item, season_description):
    title = clean_text(item.select_one('.list-item-content__title'))
    details = item.select_one('.list-item-content__description')
    paragraphs = details.select('p') if details else []
    date_time_text = clean_text(paragraphs[0]) if paragraphs else ''
    venue = clean_text(paragraphs[-1]) if len(paragraphs) > 1 else ''
    ticket_link = item.select_one('a.list-item-content__button[href]')
    event_date = parse_date(date_time_text)

    if not title or not event_date or not venue or ticket_link is None:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': ticket_link['href'],
        'time_from': parse_time(date_time_text),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': season_description or title,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_context_item(item, season_description):
    description = BeautifulSoup(item.get('description', ''), 'html.parser')
    paragraphs = description.select('p')
    date_time_text = clean_text(paragraphs[0]) if paragraphs else ''
    venue = clean_text(paragraphs[-1]) if len(paragraphs) > 1 else ''
    title = re.sub(r'\s+', ' ', item.get('title', '')).strip()
    url = item.get('button', {}).get('buttonLink', '').strip()
    event_date = parse_date(date_time_text)
    if not title or not event_date or not venue or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_time_text),
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': season_description or title,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RomeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='romesymphony_org',
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
        try:
            response = requests.get(SEASON_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Rome Symphony Orchestra season',
                event='crawler_fetch_failed',
                level='error',
                url=SEASON_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        season_heading = soup.find(
            lambda tag: tag.name in {'h1', 'h2', 'h3'}
            and 'season' in clean_text(tag).lower()
        )
        season_description = ''
        if season_heading:
            description_block = season_heading.find_next('div', class_='sqs-html-content')
            season_description = clean_text(description_block)

        records = []
        carousel = soup.select_one('[data-controller="UserItemsListCarousel"][data-current-context]')
        if carousel:
            try:
                context = json.loads(carousel['data-current-context'])
            except (json.JSONDecodeError, TypeError) as error:
                log_message(
                    'Failed to parse Rome Symphony Orchestra carousel data',
                    event='crawler_parse_failed',
                    level='warning',
                    url=SEASON_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                context = {}
            for item in context.get('userItems', []):
                record = parse_context_item(item, season_description)
                if record:
                    records.append(record)

        if not records:
            for item in soup.select('li.user-items-list-carousel__slide'):
                record = parse_item(item, season_description)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    RomeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
