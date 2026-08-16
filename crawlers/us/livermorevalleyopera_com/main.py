import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://livermorevalleyopera.com/'
PAGES_API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Livermore Valley Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}
MONTH_PATTERN = '|'.join(MONTHS)

VENUES = {
    'Bankhead Theater': ('Bankhead Theater', 'Livermore'),
    'Garré Vineyard & Winery': ('Garré Vineyard & Winery', 'Livermore'),
    'Garre Vineyard & Winery': ('Garré Vineyard & Winery', 'Livermore'),
}

NON_EVENT_SLUGS = {
    'auditions',
    'past-productions',
    'tickets',
    'tickets-2',
    'upcoming-events',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def occurrence_dates(text):
    """Expand the site's compact date lists, including lists spanning months."""
    matches = re.finditer(
        rf'\b({MONTH_PATTERN})\b(?P<body>.{{0,80}}?)\b(?P<year>20\d{{2}})\b',
        text,
        flags=re.DOTALL,
    )
    for match in matches:
        year = int(match.group('year'))
        phrase = match.group(0)[: match.group(0).rfind(str(year))]
        current_month = None
        results = []
        for token in re.findall(
            rf'\b(?:{MONTH_PATTERN})\b|\b\d{{1,2}}(?:st|nd|rd|th)?\b',
            phrase,
            flags=re.IGNORECASE,
        ):
            if token in MONTHS:
                current_month = MONTHS[token]
                continue
            if current_month is None:
                continue
            try:
                day_number = int(re.sub(r'\D', '', token))
                results.append(date(year, current_month, day_number).isoformat())
            except ValueError:
                results = []
                break
        if results:
            return list(dict.fromkeys(results))
    return []


def event_page_records(page):
    if page.get('slug') in NON_EVENT_SLUGS:
        return []
    title = clean_text(html.unescape(page.get('title', {}).get('rendered', '')))
    body = clean_text(page.get('content', {}).get('rendered', ''))
    url = page.get('link', '').strip()
    if not title or not body or not url:
        return []

    dates = occurrence_dates(body)
    if not dates:
        return []

    # A concrete event page repeats its title before its first occurrence date.
    # This rejects ticket, season, and archive overview pages that happen to
    # mention several productions in their body.
    first_date_match = re.search(rf'\b(?:{MONTH_PATTERN})\b', body)
    title_position = body.casefold().find(title.casefold())
    is_event_path = '/event/' in url
    if (
        not first_date_match
        or (not is_event_path and (title_position < 0 or title_position > first_date_match.start()))
    ):
        return []

    venue = city = None
    for marker, location in VENUES.items():
        if marker.casefold() in body.casefold():
            venue, city = location
            break
    if not venue or not city:
        return []

    time_from = None
    # Preserve a time only when the page describes one occurrence. A single
    # time beside a compact multi-date run is not safely attributable to all
    # performances.
    if len(dates) == 1:
        time_match = re.search(
            rf'\b(?:{MONTH_PATTERN})\b.{{0,35}}?20\d{{2}}\s*'
            r'(?:\bat\s+|@\s*|[•]\s*)(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b',
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if time_match:
            hour = int(time_match.group(1)) % 12
            if time_match.group(3).upper() == 'PM':
                hour += 12
            time_from = f'{hour:02d}:{int(time_match.group(2) or 0):02d}'

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': body,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class LivermoreValleyOperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='livermorevalleyopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        try:
            response = requests.get(
                PAGES_API_URL,
                params={
                    'per_page': 100,
                    'page': 1,
                    '_fields': 'id,link,slug,title,content',
                },
                headers=HEADERS,
                timeout=60,
            )
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch event pages',
                event='crawler_request_failed',
                level='error',
                url=PAGES_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for page in pages:
            records.extend(event_page_records(page))
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    LivermoreValleyOperaComCrawler().run()


if __name__ == '__main__':
    main()
