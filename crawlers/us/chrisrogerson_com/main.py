import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chrisrogerson.com/'
SOURCE = 'Chris Rogerson'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
PAST_EVENTS_URL = urljoin(SOURCE_URL, 'past-events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRIES = {
    'france': 'FR',
    'italy': 'IT',
    'japan': 'JP',
    'spain': 'ES',
    'u.s. virgin islands': 'VI',
}

US_STATE_NAMES = {
    'new hampshire': 'NH',
    'new york': 'NY',
    'north carolina': 'NC',
}

VENUE_WORDS = re.compile(
    r'\b(?:auditorium|center|centre|church|hall|library|museum|pavilion|'
    r'sanctuary|studio|theater|theatre|building|artpark|rivermead)\b',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(A\.?M\.?|P\.?M\.?)\b', re.I)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200d', '').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_dates(value):
    value = clean_text(value).replace('–', '-').replace('—', '-')
    formats = ('%B %d, %Y', '%b %d, %Y', '%m/%d/%Y')
    for pattern in formats:
        try:
            return [datetime.strptime(value, pattern).date().isoformat()]
        except ValueError:
            pass

    numeric_range = re.fullmatch(
        r'(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2}),?\s*(20\d{2})', value
    )
    named_range = re.fullmatch(
        r'([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),?\s*(20\d{2})', value, re.I
    )
    try:
        if numeric_range:
            start = date(
                int(numeric_range.group(5)),
                int(numeric_range.group(1)),
                int(numeric_range.group(2)),
            )
            end = date(
                int(numeric_range.group(5)),
                int(numeric_range.group(3)),
                int(numeric_range.group(4)),
            )
        elif named_range:
            month = datetime.strptime(named_range.group(1)[:3].title(), '%b').month
            start = date(int(named_range.group(4)), month, int(named_range.group(2)))
            end = date(int(named_range.group(4)), month, int(named_range.group(3)))
        else:
            return []
    except ValueError:
        return []

    if end < start or (end - start).days > 7:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower().startswith('p'):
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_location(lines):
    lines = [
        clean_text(part)
        for line in lines
        for part in clean_text(line).splitlines()
        if clean_text(part)
    ]
    joined = ' | '.join(lines)

    location_match = re.search(
        r'(?P<city>[A-Za-z][A-Za-z .\-\'’]+?),\s*'
        r'(?P<region>D\.?C\.?|[A-Z]{2})(?=\s*(?:,|\||$))',
        joined,
    )
    country_code = 'US'
    if not location_match:
        names = '|'.join(re.escape(name) for name in (*COUNTRIES, *US_STATE_NAMES))
        location_match = re.search(
            rf'(?P<city>[A-Za-z][A-Za-z .\-\'’]+?),\s*(?P<region>{names})(?=\s*(?:,|\||$))',
            joined,
            re.I,
        )
        if not location_match:
            return None
        region = location_match.group('region').lower()
        country_code = COUNTRIES.get(region, 'US')
    elif location_match.group('region').upper() == 'VI':
        country_code = 'VI'

    city = clean_text(location_match.group('city')).strip(' ,|')
    # Narrative descriptions commonly introduce the location with "in".
    city = re.sub(r'^.*\b(?:at|in)\s+', '', city, flags=re.I).strip()
    if not city:
        return None

    before = joined[:location_match.start()].strip(' ,|')
    parts = [TIME_RE.sub('', part).strip(' ,.-') for part in before.split('|')]
    parts = [part for part in parts if part]
    venue = next((part for part in reversed(parts) if VENUE_WORDS.search(part)), '')
    if not venue and parts:
        venue = parts[-1]
    venue = re.sub(r'^.*?\bperforms?\b.*?\bat\s+', '', venue, flags=re.I).strip(' ,.-')
    venue = re.sub(r'^(?:at|in)\s+', '', venue, flags=re.I).strip()
    if (
        not venue
        or venue.casefold() == city.casefold()
        or len(venue) > 160
        or re.match(r'^\d+\s+', venue)
        or re.search(r'\b(?:avenue|boulevard|road|street)\b', venue, re.I)
    ):
        return None
    return venue, city, country_code


def list_items(soup, wide=False):
    selector = '.features-v4.wide.w-dyn-items' if wide else '.features-v4.w-dyn-items:not(.wide)'
    container = soup.select_one(selector)
    return container.select(':scope > .w-dyn-item') if container else []


def item_key(item):
    date_node = item.select_one('.feature-v4-number > div')
    title_node = item.select_one('.feature-v4-info h4')
    return clean_text(date_node), clean_text(title_node)


def parse_item(item, enrichment):
    raw_date, title = item_key(item)
    dates = parse_dates(raw_date)
    link = item.select_one('.feature-v4-info a[href]')
    if not title or not dates or link is None:
        return []

    detail_url = urljoin(SOURCE_URL, link.get('href', ''))
    rich_text = item.select_one('.feature-v4-info .w-richtext')
    lines = [clean_text(node) for node in rich_text.select('p')] if rich_text else []

    extra = enrichment.get((raw_date, title))
    performer = ''
    if extra:
        detail = extra.select_one('.feature-v4-info h4.event-detail-2')
        performer = clean_text(detail)
        extra_rich = extra.select_one('.feature-v4-info .w-richtext')
        extra_lines = [clean_text(node) for node in extra_rich.select('p')] if extra_rich else []
        if extra_lines:
            lines = extra_lines

    location = parse_location(lines)
    if not location:
        log_message(
            'Skipped Chris Rogerson event with no defensible venue and city',
            event='crawler_item_skipped',
            level='warning',
            url=detail_url,
            error_type='IncompleteEventData',
            error_message='Could not extract both venue and city',
        )
        return []

    venue, city, country_code = location
    description_parts = [part for part in (performer, '\n'.join(lines)) if part]
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': detail_url,
            'time_from': parse_time('\n'.join(lines)),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class ChrisRogersonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chrisrogerson_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        soups = []
        for url in (EVENTS_URL, PAST_EVENTS_URL):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Chris Rogerson events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            soups.append(BeautifulSoup(response.text, 'html.parser'))

        enrichment = {}
        for soup in soups:
            for item in list_items(soup, wide=True):
                enrichment[item_key(item)] = item

        records = []
        seen_items = set()
        for soup in soups:
            for item in list_items(soup):
                key = item_key(item)
                if key in seen_items:
                    continue
                seen_items.add(key)
                records.extend(parse_item(item, enrichment))

        unique_records = {}
        for record in records:
            key = (
                record['title'].casefold(), record['date'], record['time_from'],
                record['venue'].casefold(), record['city'].casefold(),
            )
            unique_records.setdefault(key, record)

        return sorted(
            unique_records.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    ChrisRogersonComCrawler().run()


if __name__ == '__main__':
    main()
