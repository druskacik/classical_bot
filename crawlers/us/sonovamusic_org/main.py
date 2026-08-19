import calendar
import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sonovamusic.org/'
SOURCE = 'Symphony Orchestra of Northern Virginia'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:-(?P<end_day>\d{1,2}))?,\s*'
    r'(?P<year>20\d{2})',
    re.I,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.I)

VENUE_CITIES = {
    'The George Washington Masonic National Memorial Theatre': 'Alexandria',
    'George Washington Masonic National Memorial Theater': 'Alexandria',
    'George Washington Masonic National Memorial Theater and Grand Hall': 'Alexandria',
    'Leonadus K. Plenty Amphitheater': 'Alexandria',
    'Charles E. Beatley, Jr. Central Library': 'Alexandria',
    'Oswald Durant Center': 'Alexandria',
    'Bishop Ireton High School': 'Alexandria',
    'Durant Arts Center': 'Alexandria',
    'Atlas Performing Arts Center': 'Washington',
    'Lutheran Church of the Reformation': 'Washington',
    'James Lee Community Center': 'Falls Church',
    'Rosslyn Spectrum Theater': 'Arlington',
    'Trinity Episcopal Church': 'Arlington',
    "Fort C. F. Smith Park's Hendry House": 'Arlington',
    'Fort C. F. Smith Park’s Hendry House': 'Arlington',
    'Arlington Temple United Methodist Church': 'Arlington',
    'Kenmore Middle School': 'Arlington',
}

NON_EVENT_SLUGS = {
    '2026-2027-season-tickets',
    'conducting-fellowship',
    'perform-with-us',
}


def clean_text(value):
    raw = str(value or '')
    text = (
        BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
        if '<' in raw
        else raw
    )
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def make_dates(match):
    month = MONTHS[match.group('month').lower()]
    year = int(match.group('year'))
    first = int(match.group('day'))
    last = int(match.group('end_day') or first)
    result = []
    for day in range(first, last + 1):
        try:
            result.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return result


def venue_parts(value, expected_count):
    value = clean_text(value).strip(' /,-')
    value = re.sub(r'^(?:at\s+)', '', value, flags=re.I)
    if expected_count > 1 and ' / ' in value:
        parts = [part.strip() for part in value.split(' / ')]
        if len(parts) == expected_count:
            return parts
    return [value] * expected_count


def records_from_event_text(text, page_url, description=None, fallback_title=None):
    text = clean_text(text)
    matches = list(DATE_RE.finditer(text))
    if not matches:
        return [], fallback_title

    title = clean_text(text[:matches[0].start()]).strip(' -–—|') or fallback_title
    if not title or len(title) > 160:
        return [], fallback_title

    final_date = matches[-1]
    trailing = text[final_date.end():]
    time_matches = list(TIME_RE.finditer(trailing))
    if not time_matches:
        return [], title
    venue_text = trailing[time_matches[-1].end():]
    venue_text = re.sub(r'^(?:\s*(?:and|/|Concert:)\s*)', '', venue_text, flags=re.I)

    dates_and_times = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        times = [parse_time(item.group(0)) for item in TIME_RE.finditer(segment)]
        dates = make_dates(match)
        if not times:
            times = [None]
        for event_date in dates:
            for event_time in times:
                dates_and_times.append((event_date, event_time))

    venues = venue_parts(venue_text, len(dates_and_times))
    records = []
    for (event_date, event_time), venue in zip(dates_and_times, venues):
        city = VENUE_CITIES.get(venue)
        if not city or venue.lower() == 'virtual concert':
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page_url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': clean_text(description) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records, title


def parse_page(page):
    slug = clean_text(page.get('slug'))
    if slug in NON_EVENT_SLUGS:
        return []
    content = page.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    paragraphs = [clean_text(element) for element in soup.find_all('p')]
    paragraphs = [text for text in paragraphs if text]
    page_url = clean_text(page.get('link'))

    is_archive = 'season' in slug
    if not is_archive:
        dated = [text for text in paragraphs if DATE_RE.search(text)]
        if len(dated) != 1:
            return []
        title = clean_text(page.get('title', {}).get('rendered'))
        date_index = paragraphs.index(dated[0])
        combined = ' '.join(paragraphs[date_index:date_index + 2])
        description = ' '.join(paragraphs[:date_index]) or None
        records, _ = records_from_event_text(
            f'{title} {combined}', page_url, description=description
        )
        return records

    records = []
    previous_title = None
    for index, paragraph in enumerate(paragraphs):
        if not DATE_RE.search(paragraph):
            continue
        description = None
        if index + 1 < len(paragraphs) and not DATE_RE.search(paragraphs[index + 1]):
            description = paragraphs[index + 1]
        parsed, previous_title = records_from_event_text(
            paragraph,
            page_url,
            description=description,
            fallback_title=previous_title,
        )
        records.extend(parsed)
    return records


class SonovamusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sonovamusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        pages = []
        page_number = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page_number,
                    '_fields': 'slug,link,title,content',
                },
                headers=HEADERS,
                timeout=45,
            )
            if response.status_code == 400 and page_number > 1:
                break
            response.raise_for_status()
            batch = response.json()
            pages.extend(batch)
            total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
            if page_number >= total_pages:
                break
            page_number += 1

        records = []
        for page in pages:
            try:
                records.extend(parse_page(page))
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse SONOVA page',
                    event='crawler_item_failed',
                    level='warning',
                    url=clean_text(page.get('link')),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SonovamusicOrgCrawler().run()


if __name__ == '__main__':
    main()
