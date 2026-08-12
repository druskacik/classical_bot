import re
from datetime import date
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mso.hu/'
SOURCE = 'Miskolci Szimfonikus Zenekar'
EVENTS_API_URL = urljoin(SOURCE_URL, 'events/fetch')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'hu-HU,hu;q=0.9,en;q=0.7',
}

# Venue names which the site presents without a city. These are stable local
# institutions used repeatedly in the orchestra's calendar.
VENUE_CITIES = {
    'zenekari székház (malom)': 'Miskolc',
    'miskolci nemzeti színház': 'Miskolc',
    'nagyboldogasszony minorita templom': 'Miskolc',
    'miskolci nagyboldogasszony minorita templom': 'Miskolc',
    'selyemréti szent istván király templom': 'Miskolc',
    'pesti vigadó': 'Budapest',
    'kisvárdai várszínház és művészetek háza': 'Kisvárda',
    'kisvárdai várszínház és művelődési központ': 'Kisvárda',
    'kisvárdai vár': 'Kisvárda',
    'homonnai várkastély': 'Humenné',
}

SLOVAK_MARKERS = ('(sk)', 'szlovákia', 'borsi', 'betlér', 'homonna')
CITY_FIRST = ('hatvan', 'szerencs', 'miskolctapolca', 'sátoraljaújhely', 'sajóbábony')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def fetch_events(session):
    # FullCalendar requires a range, although the first-party endpoint currently
    # returns the complete published archive. Wide bounds also remain correct if
    # the server starts applying the parameters in the future.
    today = date.today()
    response = session.get(
        EVENTS_API_URL,
        params={
            'start': '2000-01-01T00:00:00Z',
            'end': f'{today.year + 6}-12-31T23:59:59Z',
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def event_url(slug):
    # Colons in a few slugs would otherwise be mistaken for a URL scheme by
    # urljoin(). Parentheses and commas are valid in the site's routes.
    encoded = quote(clean_text(slug).lstrip('/'), safe='(),-')
    return urljoin(SOURCE_URL, f'esemenyek/{encoded}')


def title_from_event(event):
    return clean_text(BeautifulSoup(event.get('title') or '', 'html.parser').select_one('h6'))


def labelled_heading(soup, label):
    for heading in soup.select('.card-body h6'):
        text = clean_text(heading)
        if text.casefold().startswith(label.casefold()):
            return clean_text(text.split(':', 1)[1] if ':' in text else '')
    return ''


def parse_times(raw):
    times = re.findall(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', raw)
    results = [f'{int(hour):02d}:{minute}' for hour, minute in times]
    # Some pages write the second performance as "és 16 óra".
    for hour in re.findall(r'(?:és|,|/)\s*([01]?\d|2[0-3])\s*óra', raw, re.IGNORECASE):
        value = f'{int(hour):02d}:00'
        if value not in results:
            results.append(value)
    return results or [None]


def parse_location(raw_venue):
    venue = clean_text(raw_venue)
    if not venue:
        return None
    folded = venue.casefold()
    country_code = 'SK' if any(marker in folded for marker in SLOVAK_MARKERS) else 'HU'

    city = VENUE_CITIES.get(folded)
    if not city and ',' in venue:
        parts = [part.strip() for part in venue.split(',') if part.strip()]
        if parts:
            city = parts[0] if parts[0].casefold() in CITY_FIRST else parts[-1]
            city = re.sub(r'\s*\((?:sk|szlovákia)\)\s*$', '', city, flags=re.IGNORECASE)
    if not city and 'miskolc' in folded:
        city = 'Miskolc'
    if not city and 'kisvárd' in folded:
        city = 'Kisvárda'
    if not city and 'borsi' in folded:
        city = 'Borša'
    if not city and 'betlér' in folded:
        city = 'Betliar'

    if not city or city.casefold() == venue.casefold():
        return None
    return venue, city, country_code


def parse_event(event, content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('main h1')) or title_from_event(event)
    raw_date = clean_text(event.get('start')).split('T', 1)[0]
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return []

    raw_datetime = labelled_heading(soup, 'Időpont')
    raw_venue = labelled_heading(soup, 'Helyszín')
    location = parse_location(raw_venue)
    if not title or not location:
        return []
    venue, city, country_code = location

    description = clean_text(BeautifulSoup(event.get('description') or '', 'html.parser')) or None
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in parse_times(raw_datetime)
    ]


def get_concerts():
    session = make_session()
    records = []
    seen_urls = set()
    for event in fetch_events(session):
        url = event_url(event.get('link_url'))
        if not event.get('link_url') or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            parsed = parse_event(event, response.content, url)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape Miskolc Symphony Orchestra concert',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if parsed:
            records.extend(parsed)
        else:
            log_message(
                'Skipped MSO concert with incomplete date or location',
                event='crawler_item_skipped',
                level='warning',
                url=url,
            )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class MsoHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mso_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MsoHuCrawler().run()


if __name__ == '__main__':
    main()
