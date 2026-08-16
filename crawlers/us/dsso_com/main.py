import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dsso.com/'
SOURCE = 'Duluth Superior Symphony Orchestra'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/concert')
CITY = 'Duluth'

# The site's first-party concert_series taxonomy. Bridge Sessions (63) is a
# popular-music series, while these series are DSSO, chamber, or youth-orchestra
# performances.
IN_SCOPE_SERIES = {8, 9, 64, 65}
IN_SCOPE_SERIES_PARAM = '8,9,64,65'
SEASONS = ('2022-23', '2023-24', '2024-25', '2025-26', '2026-27')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n')
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response


def canonical_url(value):
    parsed = urlparse(value)
    return parsed._replace(scheme='https', query='', fragment='').geturl()


def season_years(session):
    mapping = {}
    for season in SEASONS:
        url = urljoin(SOURCE_URL, f'{season}-concerts/')
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch DSSO season page',
                event='crawler_season_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        start_year = int(season[:4])
        for link in BeautifulSoup(response.text, 'html.parser').select('a[href*="/concert/"]'):
            event_url = canonical_url(urljoin(url, link.get('href')))
            # Later season pages supersede stale links retained on older pages.
            mapping[event_url] = start_year
    return mapping


def find_label_section(soup, label):
    for heading in soup.find_all(['h1', 'h2', 'h3', 'h4']):
        if clean_text(heading.get_text(' ', strip=True)).lower() != label.lower():
            continue
        section = heading.find_parent('section')
        if section:
            return section
    return None


def parse_when(soup):
    section = find_label_section(soup, 'When')
    if not section:
        return None
    values = [clean_text(node.get_text(' ', strip=True)) for node in section.find_all('h2')]
    values = [value for value in values if value and value.lower() not in {'when', 'where'}]
    month_index = next(
        (index for index, value in enumerate(values) if re.fullmatch(r'[A-Za-z]+', value)),
        None,
    )
    if month_index is None or month_index + 1 >= len(values):
        return None
    month = None
    for pattern in ('%B', '%b'):
        try:
            month = datetime.strptime(values[month_index].rstrip('.'), pattern).month
            break
        except ValueError:
            continue
    if values[month_index].lower().startswith('sept'):
        month = 9
    if month is None:
        return None
    day_text = values[month_index + 1]
    time_text = ' '.join(values[month_index + 2:])
    ordinal_days = re.findall(r'\b(\d{1,2})(?:st|nd|rd|th)\b', day_text, re.I)
    if ordinal_days:
        days = [int(value) for value in ordinal_days]
        time_text = f'{day_text} {time_text}'
    else:
        days = [int(value) for value in re.findall(r'\d{1,2}', day_text)]
    if not days:
        days = [int(value) for value in re.findall(r'\b(\d{1,2})(?:st|nd|rd|th)\b', time_text, re.I)]
    if not days:
        return None
    # Remove range end-times: they are not separate performances.
    start_times_text = re.sub(
        r'\s*-\s*\d{1,2}(?::\d{2})?\s*[AP]M\b', '', time_text, flags=re.I
    )
    times = []
    for match in re.finditer(
        r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])M\b', start_times_text, re.I
    ):
        hour, minute, meridiem = match.groups()
        hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
        times.append(f'{hour:02d}:{int(minute or 0):02d}')
    if len(days) == 1 and len(times) > 1:
        days *= len(times)
    if not times:
        times = [None] * len(days)
    elif len(times) < len(days):
        times.extend([times[-1]] * (len(days) - len(times)))
    return [(month, day, time_from) for day, time_from in zip(days, times)]


def parse_location(soup):
    section = find_label_section(soup, 'Where')
    if not section:
        return None, None
    headings = section.find_all('h2')
    for index, heading in enumerate(headings):
        if clean_text(heading.get_text(' ', strip=True)).lower() != 'where':
            continue
        if index + 1 >= len(headings):
            continue
        lines = [clean_text(part) for part in headings[index + 1].stripped_strings]
        lines = [line for line in lines if line]
        if not lines:
            continue
        location_text = ', '.join(lines)
        venue = re.split(r',\s*\d', lines[0], maxsplit=1)[0].strip()
        match = re.search(r'(?:^|,\s*)([^,]+),\s*[A-Z]{2}(?:\s+\d{5})?', location_text)
        city = match.group(1).strip() if match else CITY
        return venue, city
    return None, None


def event_year(item, event_url, month, season_mapping):
    explicit_year = re.search(r'/(?:[^/]*?)(20\d{2})(?:/|$)', event_url)
    if explicit_year and month >= 7:
        return int(explicit_year.group(1))
    start_year = season_mapping.get(event_url)
    if start_year is not None:
        return start_year if month >= 7 else start_year + 1
    published = datetime.fromisoformat(item['date'])
    return published.year + (1 if month < published.month else 0)


def parse_event(item, soup, season_mapping):
    occurrences = parse_when(soup)
    venue, city = parse_location(soup)
    title = clean_text(item.get('title', {}).get('rendered'))
    event_url = canonical_url(item.get('link', ''))
    if not occurrences or not title or not event_url or not venue or not city:
        return []
    description = clean_text(item.get('content', {}).get('rendered')) or None
    records = []
    for month, day, time_from in occurrences:
        year = event_year(item, event_url, month, season_mapping)
        try:
            event_date = datetime(year, month, day).date().isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class DssoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dsso_com',
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
        session = make_session()
        try:
            items, response = get_json(
                session,
                API_URL,
                params={
                    'per_page': 100,
                    'page': 1,
                    'concert_series': IN_SCOPE_SERIES_PARAM,
                },
            )
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            for page in range(2, total_pages + 1):
                page_items, _ = get_json(
                    session,
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        'concert_series': IN_SCOPE_SERIES_PARAM,
                    },
                )
                items.extend(page_items)
            seasons = season_years(session)
            records = []
            for item in items:
                if not IN_SCOPE_SERIES.intersection(item.get('concert_series') or []):
                    continue
                url = canonical_url(item.get('link', ''))
                try:
                    detail = session.get(url, timeout=45)
                    detail.raise_for_status()
                    event_records = parse_event(
                        item, BeautifulSoup(detail.text, 'html.parser'), seasons
                    )
                    if event_records:
                        records.extend(event_records)
                    else:
                        log_message(
                            'Skipped incomplete DSSO concert',
                            event='crawler_event_incomplete',
                            level='warning',
                            url=url,
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch DSSO concert detail',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        finally:
            session.close()
        if not records:
            log_message(
                'No DSSO concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=API_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    DssoComCrawler().run()


if __name__ == '__main__':
    main()
