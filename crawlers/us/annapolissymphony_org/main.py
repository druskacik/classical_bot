import html
import re
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://annapolissymphony.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mc_event'
SOURCE = 'Annapolis Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The event post type also contains talks, recordings, subscriptions, sales,
# and Friends of the ASO fundraisers. Physical venue evidence keeps those
# records out where possible; the potential-event classifier is the final
# authority for the remaining mixed candidate feed.
VENUES = (
    (r'\bMaryland Hall(?: for the Creative Arts)?\b', 'Maryland Hall for the Creative Arts', 'Annapolis'),
    (r'\bSeverna Park (?:High School|HS)\b', 'Severna Park High School', 'Severna Park'),
    (r'\bQuiet Waters Park\b', 'Quiet Waters Park', 'Annapolis'),
    (r'\bDowns (?:Memorial )?Park\b', 'Downs Memorial Park', 'Pasadena'),
    (r'\bForward Brewing\b', 'Forward Brewing', 'Annapolis'),
    (r'\bMusic Center at Strathmore\b|\bStrathmore Music Center\b|\bThe Music Center at Strathmore\b',
     'The Music Center at Strathmore', 'Bethesda'),
    (r'\bBowie State University(?: Fine and Performing Arts Center)?\b',
     'Bowie State University Fine and Performing Arts Center', 'Bowie'),
    (r'\bUnitarian Universalist Church of Annapolis\b',
     'Unitarian Universalist Church of Annapolis', 'Annapolis'),
    (r'\bFirst Christian Community Church\b', 'First Christian Community Church', 'Annapolis'),
    (r'\bSts?\.? Constantine and Helena Greek Orthodox Church\b',
     'Saints Constantine and Helen Greek Orthodox Church', 'Annapolis'),
    (r'\bTemple Beth Shalom\b', 'Temple Beth Shalom', 'Arnold'),
    (r'\bPaca House Terrace\s*(?:&|and)\s*Gardens\b', 'Paca House and Garden', 'Annapolis'),
    (r'\bLive Arts Maryland at Westfield Annapolis Mall\b',
     'Westfield Annapolis Mall', 'Annapolis'),
)

MONTHS = {
    name.lower(): number for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    ) if name
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def api_events(session):
    events = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,title,xdgp_genre,ACF',
            },
        )
        batch = response.json()
        events.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return events


def valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return None


def display_dates(value):
    """Return only explicit occurrences, never every day of a long range."""
    value = clean_text(value)
    iso_dates = re.findall(r'\b(20\d{2}-\d{2}-\d{2})\b', value)
    if len(iso_dates) == 1:
        try:
            date.fromisoformat(iso_dates[0])
            return iso_dates
        except ValueError:
            return []
    if len(iso_dates) == 2:
        try:
            start, end = map(date.fromisoformat, iso_dates)
        except ValueError:
            return []
        # The site's two- and three-night concert runs use short inclusive
        # ranges. Longer ranges have historically represented video access.
        if timedelta(0) <= end - start <= timedelta(days=2):
            return [(start + timedelta(days=offset)).isoformat()
                    for offset in range((end - start).days + 1)]
    short_range = re.search(
        r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s+(20\d{2})\b',
        value,
        re.I,
    )
    if short_range:
        start = date(
            int(short_range.group(4)),
            MONTHS[short_range.group(1).lower()],
            int(short_range.group(2)),
        )
        end = date(start.year, start.month, int(short_range.group(3)))
        if timedelta(0) <= end - start <= timedelta(days=2):
            return [(start + timedelta(days=offset)).isoformat()
                    for offset in range((end - start).days + 1)]
    match = re.search(
        r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b',
        value,
        re.I,
    )
    if not match:
        return []
    result = valid_date(match.group(3), MONTHS[match.group(1).lower()], match.group(2))
    return [result] if result else []


def performance_dates(soup):
    performances = []
    for day_node in soup.select('.xdgp-calendar-single .day--has-events[id^="day-"]'):
        try:
            timestamp = int(day_node['id'].split('-', 1)[1])
            event_date = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        except (KeyError, ValueError, OverflowError):
            continue
        times = [clean_text(node) for node in day_node.select('.action__time')]
        for value in times or [None]:
            performances.append((event_date, parse_time(value or '')))
    return performances


def parse_time(text):
    match = re.search(
        r'\b(1[0-2]|0?\d)(?::([0-5]\d))?'
        r'(?:\s*[-–]\s*(?:1[0-2]|0?\d)(?::[0-5]\d)?)?\s*([ap])\.?m\.?',
        text,
        re.I,
    )
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def venue_and_city(text):
    for pattern, venue, city in VENUES:
        if re.search(pattern, text, re.I):
            return venue, city
    return None, None


def event_records(session, item):
    url = item.get('link')
    acf = item.get('ACF') or {}
    title = clean_text((item.get('title') or {}).get('rendered'))
    description = clean_text(acf.get('long_desc') or acf.get('short_desc'))
    if not url or not title or not description:
        return []
    if re.search(
        r'\b(?:ASO Chat|WBJC|WETA TV|on-demand|subscription|household pass|'
        r'ticket sale|gift certificate|trip info session|pre-concert lecture)\b',
        title,
        re.I,
    ):
        return []

    dates = display_dates(acf.get('display_dates_sort') or acf.get('display_dates_long'))
    performances = []
    # Current detail pages retain a structured performance calendar with one
    # ticketing instance per exact date/time. Archives usually remove it, so
    # their stable ACF date and description fields are sufficient and avoid a
    # request for every historical page.
    if not dates or any(event_date >= date.today().isoformat() for event_date in dates):
        try:
            soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
            page_description = clean_text(soup.select_one('.event__description'))
            if page_description:
                description = page_description
            performances = performance_dates(soup)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Annapolis Symphony event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    venue, city = venue_and_city(description)
    if not venue or not city:
        return []

    if not performances:
        time_from = parse_time(description[:800])
        performances = [(event_date, time_from) for event_date in dates]

    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date, time_from in performances]


class AnnapolisSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='annapolissymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        for item in api_events(session):
            records.extend(event_records(session, item))
        unique = {(record['url'], record['date'], record['time_from']): record for record in records}
        return sorted(
            unique.values(),
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    AnnapolisSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
