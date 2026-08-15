import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://k-v.se/'
ARCHIVE_URL = f'{SOURCE_URL}konserter/'
SOURCE = 'Kammarmusikens Vänner'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1, 'februari': 2, 'mars': 3, 'april': 4, 'maj': 5,
    'juni': 6, 'juli': 7, 'augusti': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'december': 12,
}

# The site has no structured address field. These are place names repeatedly
# used in its own venue and concert copy, ordered longest-first.
CITIES = (
    'Stockholm', 'Funäsdalen', 'Bruksvallarna', 'Hälleviksstrand',
    'Östersund', 'Undersåker', 'Marstrand', 'Skärhamn', 'Grundsund',
    'Mollösund', 'Ljusnedal', 'Falsterbo', 'Skanör', 'Vaxholm', 'Lysekil',
    'Göteborg', 'Tännäs', 'Duved', 'Frösön', 'Åre', 'Fjällnäs', 'Öckerö',
    'Gullholmen', 'Rådmansö', 'Resarö', 'Blidö', 'Røros', 'Röros',
)

NON_EVENTS = re.compile(
    r'\b(fjällpass|festivalpass|hela dagen|en dag på|gratis buss|dubbelbiljett|'
    r'dobbeltbillet|guldstolen|festivalfika|music guide|musikguide)\b', re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    soup = BeautifulSoup(response.text, 'html.parser')
    # A broken optional "participants" component currently makes many complete
    # detail pages return 500 after the concert body has already been rendered.
    if response.status_code >= 400 and not soup.select_one('main h1'):
        response.raise_for_status()
    return soup


def archive_urls(session):
    """The archive renders upcoming and all retained past concerts together."""
    soup = get_soup(session, ARCHIVE_URL)
    urls = {
        link.get('href', '').split('#')[0]
        for link in soup.select('article h3 a[href*="/konserter/"]')
    }
    return sorted(url for url in urls if url.rstrip('/') != ARCHIVE_URL.rstrip('/'))


def labelled_value(soup, label):
    for node in soup.find_all(string=lambda value: value and clean_text(value).upper() == label):
        parent = node.parent
        if parent and parent.parent:
            values = [clean_text(child) for child in parent.parent.find_all(recursive=False)]
            values = [value for value in values if value and value.upper() != label]
            if values:
                return values[-1]
    return ''


def parse_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+([a-zåäö]+),?\s+(\d{4})(?:\s*-\s*(?:kl\.?\s*)?(\d{1,2})[.:](\d{2}))?',
        value.lower(),
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_from = None
    if match.group(4):
        hour, minute = int(match.group(4)), int(match.group(5))
        if hour < 24 and minute < 60 and not (hour == 0 and minute == 0):
            time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def resolve_city(venue, description):
    evidence = f'{venue}\n{description[:2500]}'
    for city in CITIES:
        if re.search(rf'(?<!\w){re.escape(city)}(?!\w)', evidence, re.I):
            canonical = 'Røros' if city in ('Røros', 'Röros') else city
            return canonical, 'NO' if canonical == 'Røros' else 'SE'
    return None, None


def parse_concert(soup, url):
    title_node = soup.select_one('main h1')
    title = clean_text(title_node)
    if not title or NON_EVENTS.search(title):
        return None

    datetime_text = labelled_value(soup, 'DATUM OCH TID')
    venue = labelled_value(soup, 'PLATS')
    event_date, time_from = parse_datetime(datetime_text)

    content = soup.select_one('main .wp-content')
    description = clean_text(content)
    city, country_code = resolve_city(venue, description)
    if not event_date or not venue or not city or venue.casefold() == city.casefold():
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = archive_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_concert(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
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


class KVSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='k_v_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KVSeCrawler().run()


if __name__ == '__main__':
    main()
