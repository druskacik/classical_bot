import re
from datetime import datetime

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://daytonperformingarts.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/production'
SOURCE = 'Dayton Performing Arts Alliance'
DEFAULT_CITY = 'Dayton'

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
OCCURRENCE_RE = re.compile(
    rf'(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:,?\s+(?P<year>20\d{{2}}))?'
    r',?\s+at\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<period>[ap]m)',
    re.IGNORECASE,
)
CITY_RE = re.compile(
    r'(?:^|[,\n])\s*([A-Za-z][A-Za-z .\'-]+),\s*(?:OH|Ohio)\s+\d{5}(?:-\d{4})?',
    re.IGNORECASE,
)


def clean_text(value, separator='\n'):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=90)
    response.raise_for_status()
    return response.json(), response.headers


def production_pages(session):
    page = 1
    while True:
        payload, headers = get_json(
            session,
            API_URL,
            params={'per_page': 100, 'page': page},
        )
        yield from payload
        total_pages = int(headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1


def detail_group(soup, icon_class):
    for group in soup.select('.sidebar__details--group'):
        if group.select_one(f'svg.{icon_class}'):
            return group
    return None


def parse_occurrences(group):
    text = clean_text(group, separator='\n')
    years = re.findall(r'\b(20\d{2})\b', text)
    fallback_year = years[0] if years else None
    occurrences = []
    for match in OCCURRENCE_RE.finditer(text):
        year = match.group('year') or fallback_year
        if not year:
            continue
        value = (
            f'{match.group("month")} {match.group("day")} {year} '
            f'{match.group("hour")}:{match.group("minute")} {match.group("period")}'
        )
        try:
            parsed = datetime.strptime(value, '%B %d %Y %I:%M %p')
        except ValueError:
            continue
        occurrences.append((parsed.date().isoformat(), parsed.strftime('%H:%M')))
    return list(dict.fromkeys(occurrences))


def parse_location(group):
    copy = group.select_one('.sidebar__details--copy') or group
    first = copy.select_one('p')
    venue = clean_text(first, separator=' ') if first else ''
    location = clean_text(copy, separator='\n')
    match = CITY_RE.search(location)
    city = clean_text(match.group(1), separator=' ') if match else DEFAULT_CITY
    return venue, city


def make_records(production, html):
    soup = BeautifulSoup(html, 'html.parser')
    calendar = detail_group(soup, 'svg--cal')
    location = detail_group(soup, 'svg--venue')
    if not calendar or not location:
        return []

    occurrences = parse_occurrences(calendar)
    venue, city = parse_location(location)
    title = clean_text((production.get('title') or {}).get('rendered'), separator=' ')
    url = (production.get('link') or '').strip()
    description = clean_text((production.get('content') or {}).get('rendered')) or None
    if not title or not url or not venue or not city:
        return []

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
        }
        for event_date, time_from in occurrences
    ]


def get_concerts():
    session = requests.Session(impersonate='chrome')
    session.headers.update({'Accept': 'application/json, text/html;q=0.9'})
    records = []
    for production in production_pages(session):
        url = (production.get('link') or '').strip()
        if not url:
            continue
        try:
            response = session.get(url, timeout=90)
            response.raise_for_status()
            records.extend(make_records(production, response.text))
        except requests.RequestsError as error:
            log_message(
                'Failed to scrape production detail',
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


class DaytonPerformingArtsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='daytonperformingarts_org',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    DaytonPerformingArtsOrgCrawler().run()


if __name__ == '__main__':
    main()
