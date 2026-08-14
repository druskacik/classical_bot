import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://solistkoret.no/'
SOURCE = 'Det Norske Solistkor'
CONCERTS_URL = urljoin(SOURCE_URL, 'konserter')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}
MONTHS = {
    'januar': 1, 'februar': 2, 'mars': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'desember': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_month_day(value):
    match = re.search(
        r'\b(\d{1,2})\.\s*(' + '|'.join(MONTHS) + r')\b',
        clean_text(value).casefold(),
    )
    if not match:
        return None
    return int(match.group(1)), MONTHS[match.group(2)]


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', clean_text(value))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def event_schedule(index_soup, today=None):
    """Return detail URLs with the years implied by the ordered upcoming feed."""
    today = today or date.today()
    cursor = today
    schedule = []
    for card in index_soup.select('main a.concert-index-single[href]'):
        url = urljoin(CONCERTS_URL, card.get('href'))
        occurrences = []
        for node in card.select('.concert-index-single-item'):
            parsed = parse_month_day(node)
            if not parsed:
                continue
            day, month = parsed
            year = cursor.year
            try:
                candidate = date(year, month, day)
                if candidate < cursor:
                    candidate = date(year + 1, month, day)
            except ValueError:
                continue
            cursor = candidate
            occurrences.append(candidate)
        if occurrences:
            schedule.append((url, occurrences))
    return schedule


def description_from(soup):
    parts = []
    for selector in ('.concert-single-intro', '.concert-single-content', '.concert-single-features'):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def parse_event(html, url, scheduled_dates):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1.concert-single-title'))
    locations = soup.select('main .concert-locations-item')
    if not title or not locations:
        return []

    unused_dates = list(scheduled_dates)
    records = []
    description = description_from(soup)
    for location in locations:
        venue = clean_text(location.select_one('.concert-location-title'))
        city = clean_text(location.select_one('.concert-location-city'))
        date_text = clean_text(location.select_one('.concert-location-date'))
        parsed = parse_month_day(date_text)
        if not venue or not city or not parsed:
            continue
        day, month = parsed
        match_index = next(
            (i for i, item in enumerate(unused_dates) if (item.day, item.month) == (day, month)),
            None,
        )
        if match_index is None:
            continue
        event_date = unused_dates.pop(match_index)
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': parse_time(date_text),
            'venue': venue,
            'city': city,
            'country_code': 'NO',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class SolistkoretNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='solistkoret_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='classical',
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CONCERTS_URL, timeout=45)
        response.raise_for_status()
        schedule = event_schedule(BeautifulSoup(response.text, 'html.parser'))

        records = []
        for url, scheduled_dates in schedule:
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                records.extend(parse_event(detail.text, url, scheduled_dates))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Solistkoret concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    return SolistkoretNoCrawler().run()


if __name__ == '__main__':
    main()
