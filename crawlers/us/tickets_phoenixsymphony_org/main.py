import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tickets.phoenixsymphony.org/'
CATALOGUE_URL = 'https://www.phoenixsymphony.org/wp-json/wp/v2/shows'
SOURCE = 'The Phoenix Symphony'
COUNTRY_CODE = 'US'

# Every show category used for public Phoenix Symphony performances.  The only
# omitted populated category is ``special-event`` (fundraisers and receptions).
PERFORMANCE_CATEGORY_IDS = (
    9, 10, 11, 40, 68, 83, 84, 98, 99, 100, 101, 102, 106, 107, 113, 114, 115
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Arizona Financial Theatre': 'Phoenix',
    'Arizona Musicfest': 'Scottsdale',
    'Chandler Center for the Arts': 'Chandler',
    'Madison Center for the Arts': 'Phoenix',
    'Mesa Arts Center': 'Mesa',
    'Musical Instrument Museum': 'Phoenix',
    'Orpheum Theatre': 'Phoenix',
    'Pinnacle Presbyterian Church': 'Scottsdale',
    'Symphony Hall': 'Phoenix',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', unescape(text).replace('\xa0', ' ')).strip()


def parse_date(value):
    value = clean_text(value)
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'([A-Za-z]+\s+\d{1,2},\s+\d{4})',
        value,
        re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def section_text(soup, section_id):
    section = soup.find(id=section_id)
    if not section:
        return ''
    return clean_text(section.get_text('\n', strip=True))


def venue_from_page(soup):
    # The masthead contains the venue on every generation of the show template.
    # Older overview panels replace the venue name with a "Read More" link.
    for venue in VENUE_CITIES:
        if soup.find(string=lambda value: value and clean_text(value) == venue):
            return venue
    for heading in soup.find_all(['h2', 'h3']):
        if clean_text(heading).lower() != 'venue':
            continue
        candidate = heading.find_next(['h2', 'h3'])
        if candidate:
            venue = clean_text(candidate)
            if venue and venue.lower() != 'schedule':
                return venue
    return ''


def parse_show(show, session=None):
    session = session or requests.Session()
    response = session.get(show['link'], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    title = clean_text(show.get('title', {}).get('rendered'))
    venue = venue_from_page(soup)
    city = VENUE_CITIES.get(venue)
    if not title or not venue or not city:
        log_message(
            'Skipping show without a resolved venue and city',
            event='crawler_record_skipped',
            level='warning',
            url=show.get('link'),
            venue=venue or None,
        )
        return []

    parts = []
    for section_id in ('overview', 'program'):
        text = section_text(soup, section_id)
        if text and text not in parts:
            parts.append(text)
    description = '\n\n'.join(parts) or None

    table = soup.select_one('#schedule table.show-schedule-table')
    if not table:
        return []

    records = []
    for row in table.select('tbody tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        event_date = parse_date(cells[0].get_text(' ', strip=True))
        if not event_date:
            continue
        ticket = row.find('a', href=re.compile(r'tickets\.phoenixsymphony\.org', re.I))
        url = ticket.get('href').strip() if ticket else show['link']
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(cells[1].get_text(' ', strip=True)),
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_catalogue(session):
    records = []
    page = 1
    while True:
        response = session.get(
            CATALOGUE_URL,
            params={
                'per_page': 100,
                'page': page,
                'show_cat': ','.join(map(str, PERFORMANCE_CATEGORY_IDS)),
                '_fields': 'id,link,title,show_cat',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    shows = fetch_catalogue(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        # requests.Session is not guaranteed to be thread-safe; each detail
        # worker therefore creates its own short-lived session.
        futures = {executor.submit(parse_show, show): show for show in shows}
        for future in as_completed(futures):
            show = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Show detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=show.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class TicketsPhoenixSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tickets_phoenixsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    TicketsPhoenixSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
