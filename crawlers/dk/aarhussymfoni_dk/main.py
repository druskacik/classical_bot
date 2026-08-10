import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.aarhussymfoni.dk/'
SOURCE = 'Aarhus Symfoniorkester'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/koncert'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'marts': 3,
    'april': 4,
    'maj': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_text(value, separator=' '):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(separator, strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        value = re.sub(r'[ \t]+', ' ', value)
        value = re.sub(r' *\n *', '\n', value)
        return re.sub(r'\n{3,}', '\n\n', value).strip()
    return re.sub(r'\s+', ' ', value).strip()


def catalogue(session):
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'desc',
                '_fields': 'id,link,title,content',
            },
            timeout=60,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        items = response.json()
        if not items:
            break
        yield from items
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1


def parse_datetime(value):
    match = re.search(
        r'\b(\d{1,2})\.\s+'
        r'(januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december)'
        r'\s+(\d{4})\s+kl\.\s*(\d{1,2})[.:](\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        )
        hour = int(match.group(4))
        minute = int(match.group(5))
        if hour > 23 or minute > 59:
            return None
    except ValueError:
        return None
    return event_date.isoformat(), f'{hour:02d}:{minute:02d}'


def find_meta(soup, heading):
    for node in soup.select('.meta-heading'):
        if clean_text(node).casefold() == heading.casefold():
            return node.parent
    return None


def primary_venue(place_block):
    paragraph = place_block.find('p') if place_block else None
    if not paragraph:
        return ''
    lines = [clean_text(line) for line in paragraph.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    kept = []
    for line in lines:
        if re.match(r'^(?:\*|NB!|Gratis adgang|Pladsbestilling)', line, re.IGNORECASE):
            break
        if re.search(r'\b(?:koncerten|spilles|afholdes)\b', line, re.IGNORECASE):
            break
        kept.append(line.rstrip(' ,'))
    return ', '.join(dict.fromkeys(kept))


def location_for(place_block, event_date):
    venue = primary_venue(place_block)
    if not venue:
        return None

    full_text = clean_text(place_block, separator='\n')
    parsed_date = date.fromisoformat(event_date)
    month_name = next(name for name, number in MONTHS.items() if number == parsed_date.month)
    date_patterns = (
        rf'\b{parsed_date.day}/0?{parsed_date.month}\b',
        rf'\b{parsed_date.day}\.\s*{month_name}\b',
    )
    # Some subscription pages describe tour performances in a note after the
    # home venue. Apply the note only to the explicitly named performance day.
    note = ''
    for pattern in date_patterns:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            # Limit matching to this date's note. Some pages list several tour
            # dates and venues in the same Sted block.
            note = full_text[match.start():match.start() + 90].casefold()
            break
    if note:
        if 'aalborg' in note:
            return 'Musikkens Hus', 'Aalborg'
        if 'skanderborg' in note or 'skanderbog' in note:
            return 'Kulturhuset Skanderborg', 'Skanderborg'
        if 'viborg' in note:
            return 'Viborg Katedralskole', 'Viborg'
        if 'holstebro' in note:
            return 'Musikteatret Holstebro', 'Holstebro'
        if 'morsø teater' in note:
            return 'Morsø Teater', 'Nykøbing Mors'

    lowered = venue.casefold()
    if 'højbjerg' in lowered or 'marselisborgskov' in lowered:
        return venue, 'Højbjerg'
    if any(
        marker in lowered
        for marker in (
            'musikhuset aarhus', 'aarhus rådhus', 'aarhus havn', 'aarhus',
            'sallings plads', 'musikhusparken', 'skanseparken',
        )
    ):
        return venue, 'Aarhus'
    return None


def description_from(item, soup):
    parts = []
    intro = soup.select_one('.koncertinformation-intro, .entry-excerpt')
    body = soup.select_one('.entry-content')
    for node in (intro, body):
        text = clean_text(node, separator='\n')
        if text and text not in parts:
            parts.append(text)

    works = find_meta(soup, 'Værker')
    works_text = clean_text(works, separator='\n')
    if works_text:
        parts.append(works_text)

    # The REST response is a reliable fallback when a historical page uses an
    # older template without the current entry-content wrapper.
    if not parts:
        fallback = BeautifulSoup(
            (item.get('content') or {}).get('rendered') or '', 'html.parser'
        )
        text = clean_text(fallback, separator='\n')
        if text:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(item):
    url = item.get('link') or ''
    if not url:
        return []
    response = make_session().get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('h1.entry-title'))
    if not title:
        title = clean_text(BeautifulSoup(
            (item.get('title') or {}).get('rendered') or '', 'html.parser'
        ))
    place_block = find_meta(soup, 'Sted')
    description = description_from(item, soup)
    records = []
    for node in soup.select('.koncertinformation-dato-fuld'):
        starts_at = parse_datetime(clean_text(node))
        if not starts_at:
            continue
        event_date, time_from = starts_at
        location = location_for(place_block, event_date)
        if not title or not location:
            continue
        venue, city = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'DK',
            'description': description,
        })
    return records


def get_concerts():
    session = make_session()
    try:
        items = list(catalogue(session))
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Aarhus Symphony concert catalogue',
            event='crawler_fetch_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event_page, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                page_records = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to parse Aarhus Symphony concert page',
                    event='crawler_item_failed',
                    level='warning',
                    url=item.get('link') or SOURCE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if not page_records:
                log_message(
                    'Skipping concert without a complete date and location',
                    event='crawler_item_skipped',
                    level='warning',
                    url=item.get('link') or SOURCE_URL,
                )
            records.extend(page_records)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class AarhusSymfoniDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aarhussymfoni_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AarhusSymfoniDkCrawler().run()


if __name__ == '__main__':
    main()
