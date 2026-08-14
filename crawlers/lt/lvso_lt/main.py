import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lvso.lt/'
EVENTS_URL = urljoin(SOURCE_URL, 'en/events')
SOURCE = 'Lithuanian State Symphony Orchestra'

# The main feed contains the site's four musical event types.  Performances
# presented elsewhere under the orchestra's own participation feed are separate.
FEED_URLS = (EVENTS_URL, f'{EVENTS_URL}/other-events-with-lvso')
MUSICAL_CATEGORY_IDS = ('43', '44', '45', '46')
DATE_FROM = '2000-01-01'
DATE_TO = '2100-12-31'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*,?\s*'
    r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))?',
    re.IGNORECASE,
)

LITHUANIAN_CITIES = (
    'Vilnius',
    'Kaunas',
    'Klaipėda',
    'Šiauliai',
    'Panevėžys',
    'Alytus',
    'Marijampolė',
    'Utena',
    'Birštonas',
    'Palanga',
    'Anykščiai',
    'Rokiškis',
    'Jonava',
)


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session, feed_url):
    urls = []
    page = 1
    while True:
        params = [
            ('date_from', DATE_FROM),
            ('date_to', DATE_TO),
            ('page', str(page)),
        ]
        if feed_url == EVENTS_URL:
            params.extend(('category[]', category_id) for category_id in MUSICAL_CATEGORY_IDS)
        soup = get_soup(session, feed_url, params=params)
        page_urls = [
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('.events_items_list a.hover[href]')
        ]
        if not page_urls:
            break
        urls.extend(page_urls)
        page += 1
    return urls


def event_properties(soup):
    properties = {}
    for row in soup.select('.event_dates > div'):
        label = row.select_one('strong')
        if not label:
            continue
        key = clean_text(label).rstrip(':').casefold()
        value = clean_text(row)
        value = re.sub(rf'^{re.escape(clean_text(label))}\s*:\s*', '', value, flags=re.I)
        properties[key] = value
    return properties


def parse_date_time(value):
    match = DATE_TIME_RE.search(value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(2):
        compact = re.sub(r'\s+', '', match.group(2)).upper()
        for pattern in ('%I:%M%p', '%I%p'):
            try:
                time_from = datetime.strptime(compact, pattern).strftime('%H:%M')
                break
            except ValueError:
                pass
    return event_date, time_from


def parse_location(value):
    if not value or re.search(r'webcast|internet|online|transliacija', value, re.I):
        return None, None, None

    city = None
    for candidate in LITHUANIAN_CITIES:
        if re.search(rf'\b{re.escape(candidate)}\b', value, re.I):
            city = candidate
            break
    if not city:
        return None, None, None

    venue = re.sub(r'\s*\([^)]*\)\s*', ' ', value).strip(' ,')
    # Some records append the address without parentheses.
    venue = re.sub(r',?\s+(?:[A-ZĄČĘĖĮŠŲŪŽ][\w.ĄČĘĖĮŠŲŪŽąčęėįšųūž-]*\s+)*(?:g\.|str\.|sq\.|pr\.)\s*\d.*$', '', venue, flags=re.I)
    if venue.casefold() == city.casefold() or not venue:
        return None, None, None
    return venue, city, 'LT'


def make_record(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('.page-header h1'))
    properties = event_properties(soup)
    date_value = properties.get('venue', '')
    for row in soup.select('.event_dates > div'):
        if row.select_one('.fa-calendar-o'):
            date_value = clean_text(row)
            break
    event_date, time_from = parse_date_time(date_value)
    venue, city, country_code = parse_location(properties.get('venue', ''))
    if not title or not event_date or not venue or not city or not country_code:
        return None

    description_parts = []
    members = soup.select_one('.content .members-list')
    if members:
        description_parts.append(clean_text(members, separator='\n'))
    for node in soup.select('.content > .text'):
        text = clean_text(node, separator='\n')
        if text:
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
    }


class LvsoLtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lvso_lt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LT',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = []
        for feed_url in FEED_URLS:
            try:
                urls.extend(listing_urls(session, feed_url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape LVSO event listing',
                    event='crawler_listing_failed',
                    level='warning',
                    url=feed_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        unique_urls = list(dict.fromkeys(urls))
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(make_record, session, url): url for url in unique_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape LVSO event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    LvsoLtCrawler().run()


if __name__ == '__main__':
    main()
