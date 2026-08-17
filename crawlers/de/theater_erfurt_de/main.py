import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-erfurt.de/'
SOURCE = 'Theater Erfurt'
PROGRAM_API = urljoin(SOURCE_URL, 'actions/site/program')
CITY = 'Erfurt'

# These are the stable first-party IDs exposed by the Spielplan filter.  The
# genre query catches eligible productions even when their series assignment
# changes; the series query also catches broadly labelled music-theatre, dance,
# festival, and concert productions.  The union deliberately goes to the
# potential-event classifier because Musical and Tanz can be out of scope.
GENRE_IDS = ['20782', '20784', '3501', '20786', '20788', '20790']
SERIES_IDS = [
    '43691', '43693', '810750', '810900', '813462', '813485', '816644',
    '818684',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def module_config(html):
    match = re.search(r'window\.Module\s*=\s*(\{.*?\});</script>', html, re.DOTALL)
    if not match:
        raise ValueError('Theater Erfurt module configuration was not found')
    return json.loads(match.group(1))


def api_post(session, path, payload, csrf_name, csrf_value):
    body = dict(payload)
    body[csrf_name] = csrf_value
    response = session.post(
        f'{PROGRAM_API}/{path}', json=body, timeout=45,
        headers={'X-Requested-With': 'XMLHttpRequest'},
    )
    response.raise_for_status()
    return response.json()


def parse_filter_range(filter_html):
    soup = BeautifulSoup(filter_html, 'html.parser')
    values = [option.get('value') for option in soup.select('#month option[value]')]
    months = []
    for value in values:
        try:
            months.append(datetime.fromisoformat(value).date())
        except (TypeError, ValueError):
            continue
    if not months:
        raise ValueError('Theater Erfurt calendar did not expose a date range')
    start = min(months)
    last = max(months)
    if last.month == 12:
        end = date(last.year + 1, 1, 1)
    else:
        end = date(last.year, last.month + 1, 1)
    return start, end


def parse_program_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    occurrences = []
    for day in soup.select('.program-day'):
        date_node = day.select_one('.date[data-date]')
        event_date = date_node.get('data-date') if date_node else None
        try:
            event_date = date.fromisoformat(event_date).isoformat()
        except (TypeError, ValueError):
            continue
        for item in day.select('.program[data-entryid], .program[data-entryId]'):
            title_link = item.select_one('.program-info h2 a[href]')
            time_location = clean_text(item.select_one('.time-location'))
            match = re.match(r'(?:(\d{1,2}:\d{2})\s*/\s*)?(.+)', time_location)
            time_from = match.group(1) if match else None
            venue = clean_text(match.group(2)) if match else ''
            title = clean_text(title_link)
            if not all((title, title_link, event_date, venue)):
                continue
            occurrences.append({
                'title': title,
                'date': event_date,
                'url': urljoin(SOURCE_URL, title_link['href']),
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
            })
    return occurrences


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    # The first half-width narrative column is the production synopsis and
    # normally includes composer/work details. Credits and ticket UI are kept out.
    for node in soup.select('main .col.col-1-2 > .text'):
        description = clean_text(node)
        if description:
            return description
    return None


class TheaterErfurtDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_erfurt_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(SOURCE_URL, timeout=45)
        response.raise_for_status()
        module = module_config(response.text)
        csrf_name = module['csrfTokenName']
        csrf_value = module['csrfTokenValue']

        initial = api_post(
            session, 'get-inner-plan',
            {
                'dateTime': datetime.now(timezone.utc).isoformat(),
                'stageIds': '', 'genreIds': GENRE_IDS,
                'eventCategoryIds': '', 'eventSeriesIds': '',
            },
            csrf_name, csrf_value,
        )
        start, end = parse_filter_range(initial['filter'])
        occurrence_map = {}

        # The endpoint returns up to ten performance days from the requested
        # date. Fixed ten-day windows cover the full first-party month range.
        query_sets = [
            {'genreIds': GENRE_IDS, 'eventSeriesIds': ''},
            {'genreIds': '', 'eventSeriesIds': SERIES_IDS},
        ]
        cursor = start
        while cursor < end:
            for filters in query_sets:
                try:
                    result = api_post(
                        session, 'get-dates-by-date',
                        {
                            'dateTime': f'{cursor.isoformat()}T00:00:00.000Z',
                            'stageIds': '', 'eventCategoryIds': '', **filters,
                        },
                        csrf_name, csrf_value,
                    )
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Theater Erfurt calendar window',
                        event='crawler_fetch_failed', level='warning',
                        url=f'{PROGRAM_API}/get-dates-by-date',
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                for day in (result.get('days') or {}).values():
                    for item in parse_program_html(day.get('html', '')):
                        key = (item['url'], item['date'], item['time_from'], item['venue'])
                        occurrence_map[key] = item
            cursor += timedelta(days=10)

        descriptions = {}
        urls = {item['url'] for item in occurrence_map.values()}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail = future.result()
                    detail.raise_for_status()
                    descriptions[url] = parse_description(detail.text)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Theater Erfurt event detail',
                        event='crawler_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records = []
        for item in occurrence_map.values():
            records.append({
                **item,
                'country_code': 'DE',
                'description': descriptions.get(item['url']),
            })
        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    TheaterErfurtDeCrawler().run()


if __name__ == '__main__':
    main()
