import calendar
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.marinalsop.com/'
CALENDAR_URL = f'{SOURCE_URL}calendar/'
ARCHIVE_URL = f'{SOURCE_URL}past-performances/'
SOURCE = 'Marin Alsop'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name.lower(): number for number, name in enumerate(calendar.month_name) if name
}
MONTHS.update({
    name.lower(): number for number, name in enumerate(calendar.month_abbr) if name
})

COUNTRIES = {
    'Argentina': 'AR', 'Austria': 'AT', 'Belgium': 'BE', 'Brazil': 'BR',
    'Czech Republic': 'CZ', 'England': 'GB', 'France': 'FR', 'Germany': 'DE',
    'Ireland': 'IE', 'Japan': 'JP', 'Netherlands': 'NL', 'Poland': 'PL',
    'Romania': 'RO', 'Scotland': 'GB', 'South Africa': 'ZA', 'Spain': 'ES',
    'Sweden': 'SE', 'UK': 'GB', 'Uruguay': 'UY', 'AT': 'AT',
    'Canary Islands': 'ES',
}

US_REGIONS = {
    'CA', 'CO', 'DC', 'IL', 'IN', 'MA', 'MD', 'NE', 'NY', 'OH', 'OK', 'PA',
    'TX', 'Maryland', 'Pennsylvania', 'Indiana', 'New York',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_location(value):
    location = clean_text(value)
    if location == 'Singapore':
        return 'Singapore', 'SG'
    if location in {'Washington, D.C.', 'Washington, DC'}:
        return 'Washington', 'US'

    parts = [part.strip() for part in location.split(',')]
    if len(parts) < 2:
        return None, None
    city, region = parts[0], parts[-1]
    if not city or any(word in city.lower() for word in ('tour', 'livestream')):
        return None, None
    if region in US_REGIONS:
        return city, 'US'
    country_code = COUNTRIES.get(region)
    return (city, country_code) if country_code else (None, None)


def month_number(value):
    return MONTHS.get(value.lower().rstrip('.'))


def make_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def parse_date_expression(value):
    """Return every date explicitly represented by a calendar row."""
    text = clean_text(value).replace('–', '-')
    time_match = re.search(r'\((\d{1,2}):(\d{2})\s*([AP]M)\)', text, re.I)
    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).upper() == 'PM':
            hour += 12
        time_from = f'{hour:02d}:{time_match.group(2)}'
    text = re.sub(r'\s*\([^)]*\)\s*', ' ', text).strip()

    year_matches = [int(year) for year in re.findall(r'\b(20\d{2})\b', text)]
    if not year_matches:
        return [], time_from
    default_year = year_matches[-1]

    # Give each named month and each explicit year to the following day tokens.
    tokens = re.findall(r'[A-Za-z]+|\d{1,4}|[-&,]', text)
    current_month = None
    current_year = default_year
    points = []
    pending_range = False
    for index, token in enumerate(tokens):
        month = month_number(token)
        if month:
            current_month = month
            continue
        if token.isdigit() and len(token) == 4:
            current_year = int(token)
            continue
        if token == '-':
            pending_range = True
            continue
        if not token.isdigit() or not current_month:
            continue
        # A day immediately followed by a year is still a day, not metadata.
        day = int(token)
        if day > 31:
            continue
        event_date = make_date(current_year, current_month, day)
        if not event_date:
            continue
        if pending_range and points:
            start = points[-1]
            if event_date < start:
                event_date = make_date(start.year + 1, current_month, day)
            if event_date:
                span = (event_date - start).days
                # Long spans on this artist calendar describe a residency or
                # tour, not a claim that a public concert occurs every day.
                # Without individual dates those rows are not concrete events.
                if span > 3:
                    return [], time_from
                points.extend(start + timedelta(days=offset) for offset in range(1, span + 1))
        else:
            points.append(event_date)
        pending_range = False

    return sorted(set(item.isoformat() for item in points)), time_from


def parse_event(item, page_url):
    title = clean_text(item.select_one('.perform-title'))
    dates, time_from = parse_date_expression(clean_text(item.select_one('.date-title')))
    locations = item.select('.city')
    if len(locations) < 2:
        return []
    city, country_code = parse_location(locations[0])
    venue = clean_text(locations[1])
    link = item.select_one('a.ticket-link[href]')
    event_id = clean_text(item.get('id'))
    url = clean_text(link.get('href')) if link else f'{page_url}#{event_id}'
    if not title or not dates or not url or not venue or not city or not country_code:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def fetch_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


class MarinalsopComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marinalsop_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        records = []
        for page_url in (CALENDAR_URL, ARCHIVE_URL):
            try:
                soup = BeautifulSoup(fetch_page(session, page_url), 'html.parser')
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Marin Alsop calendar page',
                    event='crawler_page_failed',
                    level='warning',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            items = soup.select('li.event-item')
            for item in items:
                parsed = parse_event(item, page_url)
                if parsed:
                    records.extend(parsed)
                else:
                    log_message(
                        'Skipped incomplete Marin Alsop calendar entry',
                        event='crawler_item_skipped',
                        level='warning',
                        url=f'{page_url}#{clean_text(item.get("id"))}',
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, city, or country is missing',
                    )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    MarinalsopComCrawler().run()


if __name__ == '__main__':
    main()
