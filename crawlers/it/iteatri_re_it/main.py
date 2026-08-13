import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.iteatri.re.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/spettacolo'
SOURCE = 'Fondazione I Teatri Reggio Emilia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def occurrence_year(day, month, published):
    """Infer the season year from the post publication date.

    The site's date blocks deliberately omit years. Posts are published when a
    season is announced, so an occurrence is the first matching calendar date
    no more than two months before publication (allowing late-added events).
    """
    threshold = published.date() - timedelta(days=62)
    for year in range(published.year - 1, published.year + 3):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= threshold:
            return candidate.isoformat()
    return None


def parse_post(item):
    content = item.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(BeautifulSoup(item.get('title', {}).get('rendered', ''), 'html.parser'))
    url = item.get('link', '').strip()
    try:
        published = datetime.fromisoformat(item['date'])
    except (KeyError, TypeError, ValueError):
        return []

    location = soup.select_one('[class*="location-tpl"]')
    venue = clean_text(location)
    if not title or not url or not venue:
        return []

    date_block = soup.select_one('[class*="date-tpl"]')
    if date_block is None:
        return []

    description = clean_text(soup)
    records = []
    pattern = re.compile(
        r'(?P<day>\d{1,2})\s+'
        r'(?P<month>gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)'
        r'(?:\s+(?P<year>20\d{2}))?'
        r'(?:\s*[-–—]\s*(?:ore\s*)?(?P<hour>\d{1,2})[.:](?P<minute>\d{2}))?',
        re.I,
    )
    for node in date_block.select('p') or [date_block]:
        for match in pattern.finditer(clean_text(node)):
            day = int(match.group('day'))
            month = MONTHS[match.group('month').casefold()]
            try:
                event_date = (
                    date(int(match.group('year')), month, day).isoformat()
                    if match.group('year')
                    else occurrence_year(day, month, published)
                )
            except ValueError:
                continue
            if event_date is None:
                continue

            time_from = None
            if match.group('hour'):
                hour, minute = int(match.group('hour')), int(match.group('minute'))
                if hour <= 23 and minute <= 59:
                    time_from = f'{hour:02d}:{minute:02d}'

            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': 'Reggio Emilia',
                'country_code': 'IT',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class IteatriReItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='iteatri_re_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        page = 1
        total_pages = None
        while True:
            try:
                response = session.get(
                    API_URL,
                    # Large pages intermittently exhaust the site's PHP worker.
                    params={'per_page': 20, 'page': page, 'orderby': 'id', 'order': 'desc'},
                    timeout=60,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                # A malformed legacy post can make WordPress fail an entire
                # batch. Fetch that offset one item at a time and skip only the
                # post the API itself cannot serialize.
                recovered = []
                start = (page - 1) * 20 + 1
                for item_page in range(start, start + 20):
                    item_response = session.get(
                        API_URL,
                        params={
                            'per_page': 1, 'page': item_page,
                            'orderby': 'id', 'order': 'desc',
                        },
                        timeout=60,
                    )
                    if item_response.ok:
                        recovered.extend(item_response.json())
                if recovered:
                    for item in recovered:
                        records.extend(parse_post(item))
                    if total_pages is not None and page < total_pages:
                        page += 1
                        continue
                log_message(
                    'Failed to fetch I Teatri performance API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            items = response.json()
            for item in items:
                records.extend(parse_post(item))

            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    IteatriReItCrawler().run()


if __name__ == '__main__':
    main()
