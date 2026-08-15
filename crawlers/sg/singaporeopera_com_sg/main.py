import re
from datetime import date

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.singaporeopera.com.sg/'
SOURCE = 'Singapore Lyric Opera'
SEASON_URLS = (
    'https://www.singaporeopera.com.sg/season2025-1',
    'https://www.singaporeopera.com.sg/season2025',
    'https://www.singaporeopera.com.sg/season',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
MONTHS = {
    month: number for number, month in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        start=1,
    )
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    """Expand the date forms used by the three first-party season pages."""
    text = clean_text(value)
    match = re.search(
        r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
        r'(?:\s*&\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?,?\s*)?'
        r'(\d{1,2})(?:\s*(?:&|-)\s*(\d{1,2}))?\s+'
        r'(' + '|'.join(MONTHS) + r')\s+(\d{4})',
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    first, second, month_name, year = match.groups()
    start_day = int(first)
    end_day = int(second or first)
    if end_day < start_day or end_day - start_day > 14:
        return []
    month = MONTHS[month_name.title()]
    try:
        return [date(int(year), month, day).isoformat()
                for day in range(start_day, end_day + 1)]
    except ValueError:
        return []


def following_fields(anchor):
    values = []
    for element in anchor.next_elements:
        if isinstance(element, Tag) and element.name == 'a' and element is not anchor:
            break
        if isinstance(element, NavigableString) and anchor not in element.parents:
            values.extend(clean_text(element).splitlines())
        if len(values) >= 6:
            break
    return [value for value in values if value]


def event_description(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Singapore Lyric Opera event detail',
            event='crawler_detail_fetch_failed', level='warning', url=url,
            error_type=type(error).__name__, error_message=str(error),
        )
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.find('main')
    return clean_text(main.get_text('\n', strip=True)) if main else None


def season_records(session, season_url):
    response = session.get(season_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    detail_cache = {}

    for anchor in soup.select('main .wixui-rich-text a[href]'):
        title = clean_text(anchor.get_text(' ', strip=True))
        url = anchor.get('href', '').strip()
        fields = following_fields(anchor)
        if not title or not url or len(fields) < 3:
            continue
        event_type, venue, date_text = fields[:3]
        dates = parse_dates(date_text)
        if not dates or not venue or 'to be advised' in venue.lower() or 'various' in venue.lower():
            continue
        # Navigation and past-season links do not have a type/venue/date triplet.
        if event_type.startswith('Season 20') or url in SEASON_URLS:
            continue
        if url not in detail_cache:
            detail_cache[url] = event_description(session, url)
        description_parts = [event_type, detail_cache[url]]
        description = '\n\n'.join(part for part in description_parts if part) or None
        for event_date in dates:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': 'Singapore',
                'country_code': 'SG',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class SingaporeOperaComSgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='singaporeopera_com_sg',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SG',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for season_url in SEASON_URLS:
            try:
                records.extend(season_records(session, season_url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Singapore Lyric Opera season',
                    event='crawler_fetch_failed', level='error', url=season_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                raise
        return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


def main():
    SingaporeOperaComSgCrawler().run()


if __name__ == '__main__':
    main()
