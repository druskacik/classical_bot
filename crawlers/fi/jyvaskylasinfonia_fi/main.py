import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jyvaskylasinfonia.fi/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Jyväskylä Sinfonia'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9',
}
WEEKDAYS = {'ma': 0, 'ti': 1, 'ke': 2, 'to': 3, 'pe': 4, 'la': 5, 'su': 6}
EVENT_LINE = re.compile(
    r'(?im)^\s*(ma|ti|ke|to|pe|la|su)\s+(\d{1,2})\.(\d{1,2})\.\s*'
    r'(.+?)\s+klo\s+([0-2]?\d)(?:[.:]([0-5]\d))?\b'
)
CITY_TOKENS = {
    'jyväskylä': 'Jyväskylä',
    'laukaa': 'Laukaa',
    'mikkeli': 'Mikkeli',
    'mäntyharju': 'Mäntyharju',
    'kuopio': 'Kuopio',
    'tampere': 'Tampere',
    'helsinki': 'Helsinki',
    'lahti': 'Lahti',
    'oulu': 'Oulu',
    'turku': 'Turku',
    'muurame': 'Muurame',
    'saarijärv': 'Saarijärvi',
    'muurasjärvi': 'Pihtipudas',
    'konginkanka': 'Äänekoski',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    prefix = f'{SOURCE_URL}konsertit/'
    return sorted({
        loc.get_text(strip=True)
        for loc in soup.find_all('loc')
        if loc.get_text(strip=True).startswith(prefix)
    })


def metadata_years(soup):
    years = []
    for selector in (
        'meta[property="og:updated_time"]',
        'meta[property="article:published_time"]',
        'meta[name="dcterms.date"]',
    ):
        value = (soup.select_one(selector) or {}).get('content', '')
        match = re.match(r'(20\d{2})', value)
        if match:
            years.append(int(match.group(1)))
    return years


def event_date(day, month, weekday, reference_years):
    candidates = set()
    for year in reference_years or [date.today().year]:
        candidates.update((year - 1, year, year + 1))
    candidates.update((date.today().year - 1, date.today().year, date.today().year + 1))
    valid = []
    for year in candidates:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate.weekday() == WEEKDAYS[weekday.casefold()]:
            distance = min(abs(year - ref) for ref in (reference_years or [date.today().year]))
            valid.append((distance, -year, candidate))
    return min(valid)[2].isoformat() if valid else None


def resolve_city(title, venue):
    searchable = f'{title} {venue}'.casefold()
    for token, city in CITY_TOKENS.items():
        if re.search(rf'(?<![a-zåäö]){re.escape(token)}', searchable):
            return city

    # The orchestra's calendar is based in Jyväskylä. Only use its home-city
    # default when neither the event heading nor its venue identifies a tour.
    if not re.search(r'kiertue|vierailu|musiikkijuhlat|kulttuuriviikot', searchable):
        return 'Jyväskylä'
    return None


def parse_concert(soup, url):
    title = clean_text(soup.select_one('h1'))
    body = soup.select_one('.field--name-body')
    description = clean_text(body)
    if not title or not description:
        return []

    years = metadata_years(soup)
    records = []
    for match in EVENT_LINE.finditer(description):
        weekday, day, month, venue, hour, minute = match.groups()
        venue = clean_text(venue).strip(' ,–-')
        event_date_value = event_date(int(day), int(month), weekday, years)
        city = resolve_city(title, venue)
        if not all((event_date_value, venue, city)) or not re.search(r'[A-Za-zÅÄÖåäö]', venue):
            continue
        records.append({
            'title': title,
            'date': event_date_value,
            'url': url,
            'time_from': f'{int(hour):02d}:{minute or "00"}',
            'venue': venue,
            'city': city,
            'country_code': 'FI',
            'description': description,
        })
    return records


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_concert(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Jyväskylä Sinfonia concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class JyvaskylaSinfoniaFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jyvaskylasinfonia_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    JyvaskylaSinfoniaFiCrawler().run()


if __name__ == '__main__':
    main()
