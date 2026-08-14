import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonia.rzeszow.pl/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/product'
SOURCE = 'Filharmonia Podkarpacka im. Artura Malawskiego w Rzeszowie'
DEFAULT_CITY = 'Rzeszów'
DEFAULT_VENUE = 'Filharmonia Podkarpacka im. Artura Malawskiego'

# Repertuar and Archiwum.  The latter is necessary because products are moved
# out of Repertuar after their performance date.
PRODUCT_CATEGORY_IDS = (16, 54)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4,
    'maja': 5, 'czerwca': 6, 'lipca': 7, 'sierpnia': 8,
    'września': 9, 'października': 10, 'listopada': 11, 'grudnia': 12,
}

CITY_FORMS = {
    'Iwoniczu-Zdroju': 'Iwonicz-Zdrój',
    'Krośnie': 'Krosno',
    'Łańcucie': 'Łańcut',
    'Niebylcu': 'Niebylec',
    'Przecławiu': 'Przecław',
    'Strzyżowie': 'Strzyżów',
    'Tarnobrzegu': 'Tarnobrzeg',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(20\d{2})\s*r?\.?',
        text.lower(),
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b(?:godz(?:ina)?\.?\s*)?(\d{1,2})[:.](\d{2})\b', text, re.I)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def first_paragraph_text(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    paragraph = soup.find('p')
    return clean_text(paragraph) if paragraph else ''


def explicit_location(first_paragraph):
    """Return text following the date/time in the event's header paragraph."""
    lines = [line.strip(' ,-') for line in first_paragraph.splitlines() if line.strip(' ,-')]
    if not lines:
        return ''
    date_line = next((index for index, line in enumerate(lines) if parse_date(line)), None)
    if date_line is None:
        return ''
    date_text = lines[date_line]
    time_match = re.search(r'\b\d{1,2}[:.]\d{2}\b', date_text)
    remainder = date_text[time_match.end():].strip(' ,-') if time_match else ''
    if remainder:
        return remainder
    return lines[date_line + 1] if date_line + 1 < len(lines) else ''


def location_from_header(first_paragraph):
    location = explicit_location(first_paragraph)
    if not location:
        return DEFAULT_VENUE, DEFAULT_CITY

    normalized = location.casefold()
    if 'filharmoni' in normalized:
        return DEFAULT_VENUE, DEFAULT_CITY

    # Most first paragraphs continue with performers.  A role separator makes
    # that explicit and is not location evidence.
    if re.search(r'\s[–—-]\s*(?:dyrygent|skrzypce|fortepian|sopran|alt|tenor|bas|gitara)', location, re.I):
        return DEFAULT_VENUE, DEFAULT_CITY

    venue_marker = re.search(
        r'\b(?:sala|bazylika|kościół|zamek|plac|dom kultury|centrum|synagoga|muzeum|dwór|pałac)\b',
        location,
        re.I,
    )
    # Touring entries sometimes contain only a town name.  That is enough for
    # the city but not for a defensible venue, so they are skipped.
    if not venue_marker and location.isupper() and len(location.split()) <= 4:
        return None, None
    city_form = next(
        (form for form in CITY_FORMS if re.search(r'\b' + re.escape(form) + r'\b', location, re.I)),
        None,
    )
    if venue_marker and city_form:
        return location, CITY_FORMS[city_form]
    if venue_marker:
        # The venue is explicit, but the city is not reliably recoverable.
        return None, None
    if not venue_marker:
        # No recognizable place was advertised, so this is a normal home-hall
        # entry whose first paragraph happens to start with performer prose.
        return DEFAULT_VENUE, DEFAULT_CITY


def parse_product(item):
    title = clean_text(item.get('title', {}).get('rendered'))
    url = item.get('link', '').strip()
    excerpt_html = item.get('excerpt', {}).get('rendered', '')
    content_html = item.get('content', {}).get('rendered', '')
    excerpt = clean_text(excerpt_html)
    content = clean_text(content_html)
    event_date = parse_date(excerpt)
    venue, city = location_from_header(first_paragraph_text(excerpt_html))
    if not title or not url or not event_date or not venue or not city:
        return None
    description_parts = []
    for value in (excerpt, content):
        if value and value not in description_parts:
            description_parts.append(value)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(excerpt),
        'venue': venue,
        'city': city,
        'country_code': 'PL',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FilharmoniaRzeszowPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_rzeszow_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        page = 1
        session = requests.Session()
        session.headers.update(HEADERS)
        while True:
            response = session.get(
                API_URL,
                params={
                    'product_cat': ','.join(map(str, PRODUCT_CATEGORY_IDS)),
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'desc',
                    '_fields': 'id,link,title,content,excerpt,product_cat',
                },
                timeout=45,
            )
            response.raise_for_status()
            for item in response.json():
                record = parse_product(item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Filharmonia Rzeszow event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=item.get('link', ''),
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, city, or URL is missing',
                    )
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']),
        )


def main():
    FilharmoniaRzeszowPlCrawler().run()


if __name__ == '__main__':
    main()
