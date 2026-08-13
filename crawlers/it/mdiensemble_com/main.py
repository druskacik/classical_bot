import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mdiensemble.com/'
SOURCE = 'mdi ensemble'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

FOREIGN_CITIES = {
    'bamberg': ('Bamberg', 'DE'),
    'konstanz': ('Konstanz', 'DE'),
    'köln': ('Köln', 'DE'),
    'cologne': ('Köln', 'DE'),
    'malmoe': ('Malmö', 'SE'),
    'malmö': ('Malmö', 'SE'),
    'madrid': ('Madrid', 'ES'),
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    match = re.search(
        r'(?P<days>\d{1,2}(?:\s*/\s*\d{1,2})*)\s+'
        r'(?P<month>[A-Za-zÀ-ÿ]+)\s+(?P<year>20\d{2})',
        value,
        re.I,
    )
    if not match:
        return []
    month = MONTHS.get(match.group('month').lower())
    if not month:
        return []
    dates = []
    for day_text in re.findall(r'\d{1,2}', match.group('days')):
        try:
            dates.append(date(int(match.group('year')), month, int(day_text)).isoformat())
        except ValueError:
            continue
    return dates


def parse_times(value):
    return [f'{int(hour):02d}:{minute}' for hour, minute in re.findall(
        r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', value
    )]


def location_row(table):
    rows = [clean_text(row) for row in table.select('tr')]
    for row in rows[1:]:
        lower = row.lower()
        if not row or parse_times(row):
            continue
        if re.search(r'bigliett|ingress|info|prenot|ascolta|stream', lower):
            continue
        return row
    return ''


def parse_location(value):
    value = clean_text(value).strip(' ,-')
    if not value or re.search(r'luogo da definire|time tbd', value, re.I):
        return None

    lower = value.lower()
    for needle, (city, country_code) in FOREIGN_CITIES.items():
        if needle in lower:
            venue = re.split(r'\s+[–—-]\s+|,\s*(?=' + re.escape(city) + r'\b)', value, maxsplit=1)[0]
            venue = re.sub(r'\s*\([^)]*(?:sweden|germany|spain)[^)]*\)\s*$', '', venue, flags=re.I)
            if venue.lower() in {needle, city.lower()}:
                return None
            return clean_text(venue), city, country_code

    without_postcode = re.sub(r'\b\d{5}\s+', '', value)
    tail = re.split(r',\s*', without_postcode)[-1]
    city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', tail).strip()
    city = re.sub(r'\s+[A-Z]{2}$', '', city).strip()
    if re.search(r'\d', city) or len(city.split()) > 4:
        dash_parts = re.split(r'\s+[–—-]\s+', without_postcode)
        if len(dash_parts) < 2:
            return None
        city = re.split(r',\s*', dash_parts[-1])[-1]
        city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city).strip()

    if not city or re.search(r'\d|via|piazza|corso|viale|largo', city, re.I):
        return None
    venue = re.split(r'\s+[–—-]\s+', value, maxsplit=1)[0].strip()
    if venue == value and ',' in value:
        venue = value.rsplit(',', 1)[0].strip()
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, 'IT'


def description_from_page(soup):
    parts = []
    for element in soup.select('main .fusion-text'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_page(page):
    url = clean_text(page.get('link'))
    if not url or '/en/' in url:
        return []
    content = page.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(content, 'html.parser')
    table = soup.select_one('table')
    if table is None:
        return []
    rows = [clean_text(row) for row in table.select('tr')]
    dates = parse_dates(rows[0] if rows else '')
    location = parse_location(location_row(table))
    heading = soup.select_one('h1, h2')
    title = clean_text(heading) or clean_text(page.get('title', {}).get('rendered'))
    if not title or not dates or not location:
        return []

    time_values = []
    for row in rows[1:]:
        time_values.extend(parse_times(row))
        if time_values:
            break
    venue, city, country_code = location
    description = description_from_page(soup)
    records = []
    for index, event_date in enumerate(dates):
        time_from = time_values[index] if index < len(time_values) else (time_values[0] if time_values else None)
        records.append({
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
        })
    return records


class MdiensembleComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mdiensemble_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
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
        page_number = 1
        total_pages = 1
        while page_number <= total_pages:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page_number,
                        '_fields': 'id,link,slug,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch mdi ensemble page catalogue',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            total_pages = int(response.headers.get('X-WP-TotalPages', 1))
            for page in response.json():
                records.extend(parse_page(page))
            page_number += 1

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    MdiensembleComCrawler().run()


if __name__ == '__main__':
    main()
