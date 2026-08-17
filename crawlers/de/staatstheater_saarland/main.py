import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.staatstheater.saarland/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalendarium')
SOURCE = 'Saarländisches Staatstheater'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Upgrade-Insecure-Requests': '1',
    'Referer': CALENDAR_URL,
}

# Every named venue in the theatre's current calendar is covered explicitly.
# This avoids assigning Saarbrücken to touring dates merely because it is the
# institution's home city.
VENUE_LOCATIONS = {
    'Alte Feuerwache': ('Saarbrücken', 'DE'),
    'Ballettsaal': ('Saarbrücken', 'DE'),
    'Chapiteau vor dem Staatstheater': ('Saarbrücken', 'DE'),
    'Congresshalle': ('Saarbrücken', 'DE'),
    'Festsaal Rathaus St. Johann': ('Saarbrücken', 'DE'),
    'Friedenskirche': ('Saarbrücken', 'DE'),
    'Großes Haus': ('Saarbrücken', 'DE'),
    'Großes Haus/Mittelfoyer': ('Saarbrücken', 'DE'),
    'Stiftskirche St. Arnual': ('Saarbrücken', 'DE'),
    'Tbilisser Platz': ('Saarbrücken', 'DE'),
    'Theater Überzwerg': ('Saarbrücken', 'DE'),
    'Théâtre National du Luxembourg': ('Luxembourg', 'LU'),
    'Weltkulturerbe Völklinger Hütte': ('Völklingen', 'DE'),
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    lines = [re.sub(r'\s+', ' ', line).strip() for line in value.replace('\xa0', ' ').splitlines()]
    return '\n'.join(line for line in lines if line)


def occurrence_url(detail_url, item):
    ical = item.select_one('a[href*="termin"]')
    if not ical:
        return detail_url
    query = parse_qs(urlsplit(ical.get('href', '')).query)
    termin = query.get('tx_sst14v_sstical[termin]', [None])[0]
    if not termin:
        return detail_url
    parts = urlsplit(detail_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({'termin': termin}), ''))


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    occurrences = []
    for item in soup.select('.date-item[data-month]'):
        title_link = item.select_one('.timetableTitle a[href]')
        venue = clean_text(item.select_one('.timetableSpielstaette'))
        location = VENUE_LOCATIONS.get(venue)
        month_year = item.get('data-month', '')
        day_match = re.search(r'\b(\d{1,2})\.', clean_text(item.select_one('.timetableDate')))
        if not title_link or not location or not day_match or not re.fullmatch(r'\d{2}-\d{4}', month_year):
            continue
        month, year = map(int, month_year.split('-'))
        try:
            event_date = date(year, month, int(day_match.group(1))).isoformat()
        except ValueError:
            continue
        time_text = clean_text(item.select_one('.timetableTime'))
        time_match = re.search(r'\b(\d{1,2}:\d{2})\b', time_text)
        detail_url = urljoin(CALENDAR_URL, title_link['href'])
        occurrences.append({
            'title': clean_text(title_link),
            'date': event_date,
            'url': occurrence_url(detail_url, item),
            'detail_url': detail_url,
            'time_from': time_match.group(1) if time_match else None,
            'venue': venue,
            'city': location[0],
            'country_code': location[1],
            'listing_description': clean_text(item.select_one('.timetableshortTeaser')) or None,
        })
    return occurrences


def parse_description(html, fallback=None):
    soup = BeautifulSoup(html, 'html.parser')
    parts = []
    for selector in (
        '.sst-start-slider-short-text',
        '.repertoireDescription',
        '.repertoireDescriptionLong',
    ):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or fallback


class StaatstheaterSaarlandCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='staatstheater_saarland',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['url'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=60)
        response.raise_for_status()
        occurrences = parse_calendar(response.text)

        descriptions = {}
        detail_urls = {item['detail_url'] for item in occurrences}
        # The host rejects parallel detail requests with HTTP 403, so keep
        # these sequential while retaining the future-based error handling.
        with ThreadPoolExecutor(max_workers=1) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_response = future.result()
                    detail_response.raise_for_status()
                    descriptions[url] = parse_description(detail_response.text)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Staatstheater event detail',
                        event='crawler_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records = []
        for item in occurrences:
            detail_url = item.pop('detail_url')
            fallback = item.pop('listing_description')
            item['description'] = descriptions.get(detail_url) or fallback
            records.append(item)
        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    StaatstheaterSaarlandCrawler().run()


if __name__ == '__main__':
    main()
