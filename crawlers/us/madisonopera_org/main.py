import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.madisonopera.org/'
SOURCE = 'Madison Opera'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
CITY = 'Madison'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<day>\d{1,2})(?:,?\s+(?P<year>20\d{2}))?'
    r'(?:\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)))?',
    re.IGNORECASE,
)
VENUES = ('Overture Hall', 'Capitol Theater', 'Garner Park', 'Madison Opera Center')
EXCLUDED_SLUGS = {
    '2026-27-season', '2025-26-season', '2425-season', '23-24', '22-23',
    '21-22', 'radio-broadcasts', 'talks', 'digital', 'oitp-history', 'learn',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'[^0-9:apm]', '', value.lower())
    try:
        return datetime.strptime(normalized, '%I:%M%p').strftime('%H:%M')
    except ValueError:
        try:
            return datetime.strptime(normalized, '%I%p').strftime('%H:%M')
        except ValueError:
            return None


def inferred_year(page_date, month, title):
    title_year = re.search(r'\b(20\d{2})\b', title)
    if title_year:
        return int(title_year.group(1))
    season_start = date.fromisoformat(page_date[:10]).year
    return season_start if month >= 7 else season_start + 1


def event_block(soup, title):
    if title.startswith('Opera in the Park'):
        return clean_text(soup.select_one('#main-content') or soup.select_one('main') or soup.body)
    candidates = soup.select('h4, p, h3, h5, div')
    matching = [clean_text(element) for element in candidates if DATE_PATTERN.search(clean_text(element))]
    return min(matching, key=len) if matching else ''


def parse_page(page, response_text):
    title = html.unescape(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text())
    soup = BeautifulSoup(response_text, 'html.parser')
    main = soup.select_one('main') or soup.select_one('#main-content') or soup.body
    description = clean_text(main)
    block = event_block(soup, title)

    if page['slug'] == 'matinee':
        block = description
        title = 'Carmen – Student Matinee'
    if not block:
        return []

    venue = next((name for name in VENUES if name.lower() in block.lower()), None)
    if venue is None:
        venue = next((name for name in VENUES if name.lower() in description[:4000].lower()), None)
    if not venue and title.startswith('Opera in the Park'):
        venue = 'Garner Park'
    if not venue:
        return []

    matches = list(DATE_PATTERN.finditer(block))
    if page['slug'] == 'matinee':
        matches = sorted(matches, key=lambda item: item.group('time') is None)

    records = []
    for match in matches:
        month = datetime.strptime(match.group('month'), '%B').month
        year = int(match.group('year')) if match.group('year') else inferred_year(
            page['date'], month, title
        )
        try:
            event_date = date(year, month, int(match.group('day')))
        except ValueError:
            continue
        if event_date.strftime('%A').lower() != match.group('weekday').lower():
            continue
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': page['link'],
            'time_from': parse_time(match.group('time')),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        if title.startswith('Opera in the Park') or page['slug'] == 'matinee':
            # Remaining Opera in the Park dates are rain dates or historical
            # recaps; the matinee page repeats its single occurrence.
            break
    return records


class MadisonOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='madisonopera_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(API_URL, params={'per_page': 100}, timeout=45)
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Madison Opera page index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for page in pages:
            if page['slug'] in EXCLUDED_SLUGS:
                continue
            api_text = clean_text(BeautifulSoup(page['content']['rendered'], 'html.parser'))
            title = html.unescape(page['title']['rendered'])
            likely_event = (
                (DATE_PATTERN.search(api_text) and any(v.lower() in api_text.lower() for v in VENUES))
                or title.startswith('Opera in the Park')
                or page['slug'] == 'matinee'
            )
            if not likely_event:
                continue
            try:
                detail = session.get(page['link'], timeout=45)
                detail.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Madison Opera production page',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=page['link'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_page(page, detail.text))

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    MadisonOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
