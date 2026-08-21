import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.marcandrehamelin.com/'
CALENDAR_URL = f'{SOURCE_URL}calendar'
SOURCE = 'Marc-André Hamelin'
SITE_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_NAMES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'brazil': 'BR',
    'canada': 'CA', 'china': 'CN', 'colombia': 'CO', 'czechia': 'CZ',
    'denmark': 'DK', 'england': 'GB', 'france': 'FR', 'georgia': 'GE',
    'germany': 'DE', 'holland': 'NL', 'italy': 'IT', 'netherlands': 'NL',
    'norway': 'NO', 'poland': 'PL', 'singapore': 'SG', 'south korea': 'KR',
    'spain': 'ES', 'switzerland': 'CH', 'united kingdom': 'GB',
    'united states': 'US', 'usa': 'US',
}

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}
CANADIAN_PROVINCES = {'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def description_from_html(value):
    soup = BeautifulSoup(value or '', 'html.parser')
    for element in soup.select('script, style, .sqs-block-button-container'):
        element.decompose()
    return clean_text(soup) or None


def local_datetime(milliseconds):
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=SITE_TIMEZONE)


def country_code_for(item):
    location = item.get('location') or {}
    parts = [
        location.get('addressCountry'), location.get('addressLine2'),
        location.get('addressLine1'), location.get('addressTitle'), item.get('title'),
    ]
    text = clean_text(' | '.join(str(part) for part in parts if part)).casefold()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', text):
            return code

    upper_text = clean_text(' | '.join(str(part) for part in parts if part)).upper()
    abbreviations = set(re.findall(r'(?:^|[ ,|])([A-Z]{2})(?=[ ,|\d]|$)', upper_text))
    if abbreviations & US_STATES:
        return 'US'
    if abbreviations & CANADIAN_PROVINCES:
        return 'CA'
    return None


def city_for(item):
    title = clean_text(item.get('title'))
    if ':' in title:
        prefix = title.split(':', 1)[0].strip()
        city = prefix.split(',', 1)[0].strip()
        if city and not re.search(r'\d', city):
            return city

    location = item.get('location') or {}
    for raw in (location.get('addressLine2'), location.get('addressLine1')):
        value = clean_text(raw)
        if not value:
            continue
        postal_city = re.search(r'\b\d{4,6}\s+([^,]+)', value)
        if postal_city:
            city = re.sub(r'\s+[A-Z]{2}$', '', postal_city.group(1)).strip()
            if city and not re.search(r'^\d', city):
                return city
        value = re.sub(r'^\d+\s+', '', value)
        value = value.split(',', 1)[0].strip()
        value = re.sub(r'\s+[A-Z]{2}\s+\d.*$', '', value).strip()
        if value and not re.search(r'^\d', value) and value.casefold() not in COUNTRY_NAMES:
            return value
    return ''


def venue_for(item, city):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    if not venue or venue.casefold() == city.casefold():
        title = clean_text(item.get('title'))
        match = re.search(r'\bat\s+(?:the\s+)?(.+)$', title, re.IGNORECASE)
        if match:
            venue = clean_text(match.group(1))
    if not venue or venue.casefold() == city.casefold():
        return ''
    return venue


def event_records(item):
    title = clean_text(item.get('title'))
    relative_url = clean_text(item.get('fullUrl'))
    url = requests.compat.urljoin(SOURCE_URL, relative_url)
    city = city_for(item)
    venue = venue_for(item, city)
    country_code = country_code_for(item)
    start = local_datetime(item.get('startDate'))
    end = local_datetime(item.get('endDate'))
    if not title or not relative_url or not city or not venue or not country_code or not start:
        return []

    occurrences = [(start.date(), start.time().replace(second=0, microsecond=0), None)]
    if end and end.date() == start.date() and end > start:
        occurrences[0] = (occurrences[0][0], occurrences[0][1], end.time().replace(second=0, microsecond=0))
    if end and end.date() != start.date():
        occurrences.append((end.date(), end.time().replace(second=0, microsecond=0), None))

    description = description_from_html(item.get('body'))
    records = []
    for event_date, event_time, end_time in occurrences:
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            'time_from': event_time.strftime('%H:%M'),
            'time_to': end_time.strftime('%H:%M') if end_time else None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MarcAndreHamelinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='marcandrehamelin_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        dedupe_subset=['url', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        offset = None
        seen_offsets = set()
        seen_items = set()
        records = []
        skipped_count = 0

        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            response = session.get(CALENDAR_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()

            items = (payload.get('upcoming') or []) + (payload.get('past') or [])
            for item in items:
                item_id = item.get('id')
                if item_id in seen_items:
                    continue
                if item_id:
                    seen_items.add(item_id)
                parsed = event_records(item)
                if not parsed:
                    skipped_count += 1
                records.extend(parsed)

            pagination = payload.get('pagination') or {}
            next_offset = pagination.get('nextPageOffset')
            if not pagination.get('nextPage') or next_offset is None or next_offset in seen_offsets:
                break
            seen_offsets.add(next_offset)
            offset = next_offset

        if skipped_count:
            log_message(
                'Skipped incomplete Marc-André Hamelin calendar events',
                event='crawler_items_skipped', level='warning', url=CALENDAR_URL,
                record_count=skipped_count, error_type='IncompleteEventData',
                error_message='Required date, city, venue, or country could not be resolved',
            )

        return sorted(records, key=lambda record: (record['date'], record['time_from'], record['title']))


def main():
    MarcAndreHamelinCrawler().run()


if __name__ == '__main__':
    main()
