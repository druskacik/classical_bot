import json
import re
from datetime import date, datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://danielbarenboim.com/'
CALENDAR_URL = f'{SOURCE_URL}calendar/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Daniel Barenboim'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = [
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
]
MONTH_NUMBERS = {month: number for number, month in enumerate(MONTHS, 1)}

# The calendar is an international touring calendar. These are used only when
# the location returned by the source (or linked venue's structured data)
# explicitly names the city.
CITY_COUNTRIES = {
    'berlin': 'DE',
    'milan': 'IT',
    'milano': 'IT',
    'paris': 'FR',
    'vienna': 'AT',
    'wien': 'AT',
    'salzburg': 'AT',
    'london': 'GB',
    'madrid': 'ES',
    'barcelona': 'ES',
    'rome': 'IT',
    'roma': 'IT',
    'munich': 'DE',
    'münchen': 'DE',
    'hamburg': 'DE',
    'lucerne': 'CH',
    'luzern': 'CH',
    'geneva': 'CH',
    'new york': 'US',
    'chicago': 'US',
    'los angeles': 'US',
    'buenos aires': 'AR',
}
CITY_NAMES = {
    'berlin': 'Berlin', 'milan': 'Milan', 'milano': 'Milan', 'paris': 'Paris',
    'vienna': 'Vienna', 'wien': 'Vienna', 'salzburg': 'Salzburg', 'london': 'London',
    'madrid': 'Madrid', 'barcelona': 'Barcelona', 'rome': 'Rome', 'roma': 'Rome',
    'munich': 'Munich', 'münchen': 'Munich', 'hamburg': 'Hamburg', 'lucerne': 'Lucerne',
    'luzern': 'Lucerne', 'geneva': 'Geneva', 'new york': 'New York', 'chicago': 'Chicago',
    'los angeles': 'Los Angeles', 'buenos aires': 'Buenos Aires',
}
COUNTRY_NAMES = {
    'argentina': 'AR', 'austria': 'AT', 'deutschland': 'DE', 'france': 'FR',
    'germany': 'DE', 'italia': 'IT', 'italy': 'IT', 'spain': 'ES',
    'switzerland': 'CH', 'united kingdom': 'GB', 'united states': 'US',
}
DOMAIN_DEFAULTS = {
    'berliner-philharmoniker.de': ('Berliner Philharmonie', 'Berlin', 'DE'),
    'philharmoniedeparis.fr': ('Philharmonie de Paris', 'Paris', 'FR'),
    'teatroallascala.org': ('Teatro alla Scala', 'Milan', 'IT'),
    'filarmonica.it': ('Teatro alla Scala', 'Milan', 'IT'),
    'boulezsaal.de': ('Pierre Boulez Saal', 'Berlin', 'DE'),
    'staatsoper-berlin.de': ('Staatsoper Unter den Linden', 'Berlin', 'DE'),
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from json_objects(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from json_objects(item)


def iso_datetime(value):
    if not isinstance(value, str):
        return None
    match = re.match(r'(20\d{2}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?', value)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    event_time = f'{match.group(2)}:{match.group(3)}' if match.group(2) else None
    return event_date, event_time


def structured_metadata(soup):
    occurrences = []
    venue = city = country_code = description = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in json_objects(data):
            item_type = item.get('@type')
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(value in ('Event', 'MusicEvent') for value in types):
                continue
            parsed = iso_datetime(item.get('startDate'))
            if parsed and parsed not in occurrences:
                occurrences.append(parsed)
            location = item.get('location')
            if isinstance(location, dict):
                venue = clean_text(location.get('name')) or venue
                address = location.get('address')
                if isinstance(address, dict):
                    city = clean_text(address.get('addressLocality')) or city
                    country = clean_text(address.get('addressCountry'))
                    country_code = COUNTRY_NAMES.get(country.casefold(), country.upper()) if country else country_code
            description = clean_text(item.get('description')) or description

    # Some venue sites publish multiple performances as time elements instead
    # of separate JSON-LD objects.
    for element in soup.select('time[datetime]'):
        parsed = iso_datetime(element.get('datetime'))
        if parsed and parsed not in occurrences:
            occurrences.append(parsed)
    return occurrences, venue, city, country_code, description


def parse_calendar_dates(value):
    match = re.fullmatch(
        r'(\d{1,2})(?:\s*&\s*(\d{1,2}))?\s+([A-Za-z]+)\s+(20\d{2})', value
    )
    if not match or match.group(3).casefold() not in MONTH_NUMBERS:
        return []
    days = [int(match.group(1))]
    if match.group(2):
        days.append(int(match.group(2)))
    results = []
    for day in days:
        try:
            results.append(date(
                int(match.group(4)), MONTH_NUMBERS[match.group(3).casefold()], day
            ).isoformat())
        except ValueError:
            return []
    return results


def location_from_calendar(venue_text):
    parenthetical = re.search(r'\(([^()]+)\)\s*$', venue_text)
    candidates = [parenthetical.group(1)] if parenthetical else []
    candidates.extend(CITY_COUNTRIES)
    normalized = venue_text.casefold()
    for candidate in candidates:
        if candidate.casefold() in normalized:
            city = candidate.strip()
            country = CITY_COUNTRIES.get(city.casefold())
            if country:
                venue = re.sub(r'\s*\([^()]+\)\s*$', '', venue_text).strip()
                return venue, CITY_NAMES.get(city.casefold(), city), country
    return None


def domain_default(url):
    hostname = (urlparse(url).hostname or '').removeprefix('www.')
    return next((value for domain, value in DOMAIN_DEFAULTS.items() if hostname.endswith(domain)), None)


def parse_card(session, card):
    title = clean_text(card.select_one('.event-title'))
    date_text = clean_text(card.select_one('.upcoming-dates'))
    venue_text = clean_text(card.select_one('.upcoming-venue'))
    link = card.select_one('a.main-ticket-link[href]')
    url = link.get('href', '').strip() if link else ''
    description = clean_text(card.select_one('.upcoming-notes')) or None
    if not title or not date_text or not url:
        return []

    occurrences = []
    detail_venue = detail_city = detail_country = detail_description = None
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        occurrences, detail_venue, detail_city, detail_country, detail_description = (
            structured_metadata(BeautifulSoup(response.text, 'html.parser'))
        )
    except requests.RequestException as error:
        log_message(
            'Failed to fetch linked Daniel Barenboim event detail',
            event='crawler_item_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    calendar_dates = parse_calendar_dates(date_text)
    if calendar_dates:
        allowed_dates = set(calendar_dates)
    else:
        range_match = re.fullmatch(r'(\d{1,2})-(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})', date_text)
        if not range_match:
            return []
        month = MONTH_NUMBERS.get(range_match.group(3).casefold())
        if not month:
            return []
        start_day, end_day, year = int(range_match.group(1)), int(range_match.group(2)), int(range_match.group(4))
        try:
            start = date(year, month, start_day).isoformat()
            end = date(year, month, end_day).isoformat()
        except ValueError:
            return []
        allowed_dates = {item[0] for item in occurrences if start <= item[0] <= end}
        if not allowed_dates:
            # A compact consecutive range on this performance-only calendar is
            # used for runs on each included day. Linked venue pages normally
            # refine non-consecutive runs before this fallback is reached.
            allowed_dates = {
                date(year, month, day).isoformat()
                for day in range(start_day, end_day + 1)
            }

    occurrence_times = {event_date: event_time for event_date, event_time in occurrences}
    location = None
    if detail_venue and detail_city:
        country = detail_country or CITY_COUNTRIES.get(detail_city.casefold())
        if country and re.fullmatch(r'[A-Za-z]{2}', country):
            location = detail_venue, detail_city, country.upper()
    location = location or location_from_calendar(venue_text) or domain_default(url)
    if not location:
        return []
    venue, city, country_code = location

    if detail_description and detail_description not in (description or ''):
        description = '\n\n'.join(filter(None, [description, detail_description]))
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': occurrence_times.get(event_date),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in sorted(allowed_dates)]


class DanielBarenboimComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='danielbarenboim_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Daniel Barenboim calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        # Direct probing confirmed that the retained archive begins in 2023;
        # exact month identifiers continue to work after years leave the UI.
        for year in range(2023, date.today().year + 2):
            for month in MONTHS:
                page = 1
                while True:
                    try:
                        response = session.post(
                            AJAX_URL,
                            data={
                                'action': 'my_action',
                                'whatever': f'{month}-{year}',
                                'paged': page,
                            },
                            headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': CALENDAR_URL},
                            timeout=45,
                        )
                        response.raise_for_status()
                    except requests.RequestException as error:
                        log_message(
                            'Failed to fetch Daniel Barenboim calendar month',
                            event='crawler_page_failed',
                            level='warning',
                            url=AJAX_URL,
                            calendar_month=f'{month}-{year}',
                            page=page,
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )
                        break
                    cards = BeautifulSoup(response.text, 'html.parser').select('.gw-gopf-col-wrap')
                    if not cards:
                        break
                    for card in cards:
                        records.extend(parse_card(session, card))
                    page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DanielBarenboimComCrawler().run()


if __name__ == '__main__':
    main()
