import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lancastersymphony.org/'
SOURCE = 'Lancaster Symphony Orchestra'
CALENDAR_URL = urljoin(SOURCE_URL, 'concert-calendar')
SUBSCRIPTION_URL = urljoin(SOURCE_URL, 'subscription')
EDUCATION_URL = urljoin(SOURCE_URL, 'student-education-concerts')
ELFSIGHT_API_URL = 'https://core.service.elfsight.com/p/boot/'
ELFSIGHT_WIDGET_ID = 'e56cc420-fd05-4635-9817-dc7051137c97'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_LINE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\s*(?:@|at|[-:|])\s*'
    r'([^\n]+)',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(\d{1,2}:\d{2})(?:\s*([AP]M))?', re.IGNORECASE)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_occurrences(text):
    occurrences = []
    for date_value, time_text in DATE_LINE_RE.findall(text):
        times = TIME_RE.findall(time_text)
        implied_meridiem = next((part.upper() for _, part in reversed(times) if part), None)
        if not re.search(r'\bor\b', time_text, re.IGNORECASE):
            times = times[:1]
        for time_value, meridiem in times:
            meridiem = meridiem.upper() if meridiem else implied_meridiem
            if not meridiem:
                continue
            try:
                value = datetime.strptime(
                    f'{date_value} {time_value} {meridiem.upper()}', '%B %d, %Y %I:%M %p'
                )
            except ValueError:
                continue
            occurrences.append((value.date().isoformat(), value.strftime('%H:%M')))
    return list(dict.fromkeys(occurrences))


def page_content(session, url):
    response = session.get(url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()
    return BeautifulSoup(payload['mainContent'], 'html.parser')


def action_url(event):
    for action in event.get('actions', []):
        value = action.get('link', {}).get('value')
        if value:
            return urljoin(SOURCE_URL, value)
    return None


def calendar_records(session):
    response = session.get(
        ELFSIGHT_API_URL,
        params={'w': ELFSIGHT_WIDGET_ID, 'page': CALENDAR_URL},
        timeout=45,
    )
    response.raise_for_status()
    widget = response.json()['data']['widgets'][ELFSIGHT_WIDGET_ID]['data']['settings']
    locations = {location['id']: location for location in widget.get('locations', [])}
    records = []

    for event in widget.get('events', []):
        url = action_url(event)
        location_ids = event.get('location') or []
        location = locations.get(location_ids[0]) if location_ids else None
        if not url or not location or not location.get('name'):
            continue

        address = location.get('address', '')
        city_match = re.search(r',\s*([^,]+),\s*PA(?:\s+\d{5})?\s*$', address)
        city = city_match.group(1).strip() if city_match else None
        if not city:
            continue

        try:
            soup = page_content(session, url)
            description = clean_text(soup)
            occurrences = parse_occurrences(description)
        except (requests.RequestException, KeyError, ValueError) as error:
            log_message(
                'Failed to fetch Lancaster Symphony event detail',
                event='crawler_detail_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            description = clean_text(BeautifulSoup(event.get('description', ''), 'html.parser'))
            start = event.get('start', {})
            occurrences = [(start.get('date'), start.get('time'))]

        for event_date, time_from in occurrences:
            if not event_date:
                continue
            records.append({
                'title': event.get('name', '').strip(),
                'date': event_date,
                'url': url,
                'time_from': time_from or None,
                'venue': location['name'].strip(),
                'city': city,
                'description': description or None,
            })
    return records


def subscription_records(session):
    """Parse announced performances that have not reached the calendar widget yet."""
    soup = page_content(session, SUBSCRIPTION_URL)
    records = []
    for paragraph in soup.find_all('p'):
        title = clean_text(paragraph)
        if not re.fullmatch(r'[A-Z][A-Za-z]+ Masterworks \d+', title):
            continue
        schedule = paragraph.find_next('li')
        program = schedule.find_next('li') if schedule else None
        occurrences = parse_occurrences(clean_text(schedule))
        description = clean_text(program) or None
        for event_date, time_from in occurrences:
            records.append({
                'title': title,
                'date': event_date,
                'url': SUBSCRIPTION_URL,
                'time_from': time_from,
                'venue': 'Gardner Theatre',
                'city': 'Lancaster',
                'description': description,
            })
    return records


def education_records(session):
    soup = page_content(session, EDUCATION_URL)
    description = clean_text(soup)
    return [{
        'title': 'Student Education Concerts',
        'date': event_date,
        'url': EDUCATION_URL,
        'time_from': time_from,
        'venue': 'McCaskey East High School',
        'city': 'Lancaster',
        'description': description or None,
    } for event_date, time_from in parse_occurrences(description)]


class LancasterSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lancastersymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for scraper in (calendar_records, subscription_records, education_records):
            try:
                records.extend(scraper(session))
            except (requests.RequestException, KeyError, ValueError) as error:
                log_message(
                    'Failed to fetch Lancaster Symphony feed',
                    event='crawler_fetch_failed',
                    level='error',
                    url=SOURCE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    LancasterSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
