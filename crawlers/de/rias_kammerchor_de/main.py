import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rias-kammerchor.de/'
CALENDAR_URL = f'{SOURCE_URL}konzertkalender/'
SITEMAP_URL = f'{SOURCE_URL}productions-sitemap.xml'
SOURCE = 'RIAS Kammerchor Berlin'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.6',
}

# Touring venues are published as a single free-text label. These unambiguous
# place names cover both the home venues and the tour destinations on the site.
PLACE_HINTS = {
    'augsburg': ('Augsburg', 'DE'),
    'bari': ('Bari', 'IT'),
    'barcelona': ('Barcelona', 'ES'),
    'berlin': ('Berlin', 'DE'),
    'budapest': ('Budapest', 'HU'),
    'castellón': ('Castellón', 'ES'),
    'chorin': ('Chorin', 'DE'),
    'dortmund': ('Dortmund', 'DE'),
    'essen': ('Essen', 'DE'),
    'halle': ('Halle (Saale)', 'DE'),
    'hamburg': ('Hamburg', 'DE'),
    'hannover': ('Hannover', 'DE'),
    'madrid': ('Madrid', 'ES'),
    'mailand': ('Mailand', 'IT'),
    'monheim am rhein': ('Monheim am Rhein', 'DE'),
    'murcia': ('Murcia', 'ES'),
    'nürnberg': ('Nürnberg', 'DE'),
    'seville': ('Sevilla', 'ES'),
    'sevilla': ('Sevilla', 'ES'),
    'zschornewitz': ('Zschornewitz', 'DE'),
}

BERLIN_VENUES = (
    'atze musiktheater',
    'bekenntniskirche',
    'berliner dom',
    'gemäldegalerie',
    'gethsemanekirche',
    'haus des rundfunks',
    'konzerthaus berlin',
    'kunsthaus dahlem',
    'nikolaikirche spandau',
    'philharmonie berlin',
    'rosenkranz-basilika',
    'st. afra',
    'st. elisabeth-kirche',
    'stadtkloster segen',
    'tangoloft',
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
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def get_text(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def production_urls(session):
    urls = set()
    calendar = get_soup(session, CALENDAR_URL)
    for link in calendar.select('a[href*="/concert/"]'):
        url = link.get('href', '').strip()
        if url:
            urls.add(url)

    # Yoast's sitemap retains production pages which have fallen off the
    # forward-looking calendar, including scrapeable past concerts.
    sitemap = get_text(session, SITEMAP_URL)
    for url in re.findall(r'<loc>(.*?)</loc>', sitemap, flags=re.DOTALL):
        url = html.unescape(url).strip()
        if '/konzerte/' in url:
            urls.add(url)
    return sorted(urls)


def parse_date(value):
    match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', value or '')
    if not match:
        return None
    day, month, year = map(int, match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(\d{1,2})[.:](\d{2})', value or '')
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def resolve_place(venue):
    lowered = venue.casefold()
    for hint, location in PLACE_HINTS.items():
        if hint.casefold() in lowered:
            return location
    if any(hint in lowered for hint in BERLIN_VENUES):
        return 'Berlin', 'DE'
    return None, None


def description_from_page(soup):
    parts = []
    works = soup.select_one('.ConcertInfo-Works')
    if works:
        text = clean_text(works)
        if text:
            parts.append(text)
    content = soup.select_one('.ConcertContent')
    if content:
        text = clean_text(content)
        if text:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_production(soup, url):
    heading = soup.select_one('.ConcertHeader h1')
    if not heading:
        return []
    title = clean_text(heading)
    subtitle = heading.find('span')
    if subtitle:
        subtitle_text = clean_text(subtitle)
        subtitle.extract()
        title = clean_text(heading)
        if subtitle_text and subtitle_text.casefold() not in title.casefold():
            title = f'{title} – {subtitle_text}'
    if not title:
        return []

    description = description_from_page(soup)
    records = []
    for item in soup.select('.ConcertInfo-Concerts-Item'):
        tags = item.select('.ConcertInfo-Concerts-Item-Header-Tag')
        venue = clean_text(tags[0]) if tags else ''
        times = item.select('.ConcertInfo-Concerts-Item-Time time')
        event_date = parse_date(clean_text(times[0])) if times else None
        time_from = parse_time(clean_text(times[1])) if len(times) > 1 else None
        city, country_code = resolve_place(venue)
        if not venue or not event_date or not city or not country_code:
            continue
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


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = production_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_production(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert production',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {}
    for record in records:
        key = (
            record['title'], record['date'], record['time_from'],
            record['venue'], record['city'],
        )
        unique.setdefault(key, record)
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class RiasKammerchorDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rias_kammerchor_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    RiasKammerchorDeCrawler().run()


if __name__ == '__main__':
    main()
