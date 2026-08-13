import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://argerich-mf.jp/'
PROGRAM_URL = 'https://argerich-mf.jp/en/program_en'
SOURCE = 'Argerich Arts Foundation'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9,ja;q=0.7',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    )
    if name
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_listing_date(text):
    match = re.search(r'\b(\d{2})/(\d{2})\s+(20\d{2})\b', text)
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_detail_date(text):
    match = re.search(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*'
        r'([A-Za-z]+)\s+(\d{1,2})\s+(20\d{2})\b',
        text,
        re.IGNORECASE,
    )
    if not match or match.group(1).lower() not in MONTHS:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))
        ).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\bStart:\s*(\d{1,2}):([0-5]\d)', text, re.IGNORECASE)
    if not match:
        # Most cards use "Doors: 18:30, 19:00 -" without a Start label.
        match = re.search(r'Doors:[^\n]*?,\s*(\d{1,2}):([0-5]\d)', text)
    if not match:
        match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*[-–]', text)
    if not match:
        return None
    hour = int(match.group(1))
    return f'{hour:02d}:{match.group(2)}' if hour < 24 else None


def split_location(value):
    value = re.sub(r'^Place\s*', '', value, flags=re.IGNORECASE)
    value = re.sub(r'\s*→\s*Access.*$', '', value, flags=re.IGNORECASE | re.DOTALL).strip(' ,')
    parenthetical = re.match(r'(.+?)\s*\(([^,()]+?)(?: City)?,\s*[^()]+\)$', value)
    if parenthetical:
        venue, city = (part.strip() for part in parenthetical.groups())
        return (venue, city) if venue and city else None
    match = re.match(r'(.+),\s*([^,]+)$', value)
    if not match:
        return None
    venue, city = (part.strip() for part in match.groups())
    if not venue or not city:
        return None
    return venue, city


def labelled_fields(soup):
    fields = {}
    for item in soup.select('.p-program__item'):
        term = clean_text(item.select_one('.p-program__term')).lower()
        description = item.select_one('.p-program__desc')
        if term and description is not None:
            fields[term] = description
    return fields


def parse_detail(soup, fallback):
    fields = labelled_fields(soup)
    title = clean_text(soup.select_one('main h1.p-hero__title')) or fallback['title']
    date_text = clean_text(fields.get('date time'))
    event_date = parse_detail_date(date_text) or fallback['date']
    location = split_location(clean_text(fields.get('place'))) or fallback['location']
    if not title or not event_date or not location:
        return None

    description_parts = []
    for key in ('programs', 'program', 'artists'):
        value = clean_text(fields.get(key))
        if value:
            description_parts.append(f'{key.title()}\n{value}')

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': fallback['url'],
        'time_from': parse_time(date_text) or fallback['time_from'],
        'venue': venue,
        'city': city,
        'country_code': 'JP',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fallback_record(item):
    venue, city = item['location']
    return {
        'title': item['title'],
        'date': item['date'],
        'url': item['url'],
        'time_from': item['time_from'],
        'venue': venue,
        'city': city,
        'country_code': 'JP',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def listing_items(soup):
    items = []
    for link in soup.select('main li > a[href*="/program/"]'):
        text = clean_text(link)
        event_date = parse_listing_date(text)
        location_match = re.search(r'(?:^|\n)Place\s+(.+)$', text, re.MULTILINE)
        location = split_location(location_match.group(1)) if location_match else None
        title_element = link.select_one('h2')
        title = clean_text(title_element)
        url = urljoin(PROGRAM_URL, link.get('href', ''))
        if event_date and location and title and url:
            items.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(text),
                'location': location,
            })
    return items


class ArgerichMfJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='argerich_mf_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
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
        try:
            current = get_soup(session, PROGRAM_URL)
            years = sorted({
                option.get('value', '')
                for option in current.select('select[name="yr"] option')
                if re.fullmatch(r'20\d{2}', option.get('value', ''))
            })
            if not years:
                raise ValueError('No programme years found')

            listings = []
            for year in years:
                soup = current if year == date.today().strftime('%Y') else get_soup(
                    session, PROGRAM_URL, params={'yr': year}
                )
                listings.extend(listing_items(soup))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Argerich programme listings',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(get_soup, session, item['url']): item for item in listings
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = parse_detail(future.result(), item)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Argerich programme detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=item['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    record = fallback_record(item)
                if record:
                    records.append(record)

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ))


def main():
    ArgerichMfJpCrawler().run()


if __name__ == '__main__':
    main()
