import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.epcc.ee/'
EVENTS_URL = urljoin(SOURCE_URL, 'kontserdid/')
LEGACY_URLS = [
    urljoin(SOURCE_URL, 'html/schedule.html'),
    urljoin(SOURCE_URL, 'html/history.html'),
]
SOURCE = 'Eesti Filharmoonia Kammerkoor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'et-EE,et;q=0.9,en;q=0.7',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

COUNTRIES = {
    'ee': 'EE', 'eesti': 'EE', 'estonia': 'EE',
    'de': 'DE', 'saksamaa': 'DE', 'germany': 'DE',
    'fi': 'FI', 'soome': 'FI', 'finland': 'FI',
    'fr': 'FR', 'prantsusmaa': 'FR', 'france': 'FR',
    'ie': 'IE', 'iirimaa': 'IE', 'ireland': 'IE',
    'it': 'IT', 'itaalia': 'IT', 'italy': 'IT',
    'lv': 'LV', 'läti': 'LV', 'latvia': 'LV',
    'lt': 'LT', 'leedu': 'LT', 'lithuania': 'LT',
    'lu': 'LU', 'luksemburg': 'LU', 'luxembourg': 'LU',
    'nl': 'NL', 'holland': 'NL', 'netherlands': 'NL',
    'no': 'NO', 'norra': 'NO', 'norway': 'NO',
    'se': 'SE', 'rootsi': 'SE', 'sweden': 'SE',
    'uk': 'GB', 'ühendkuningriik': 'GB', 'united kingdom': 'GB',
    'us': 'US', 'usa': 'US',
}

# Location strings are free text. These distinctive place-name forms cover the
# choir's Estonian halls and the touring venues in the published calendar.
CITY_HINTS = {
    'tallinn': ('Tallinn', 'EE'), 'tallinna': ('Tallinn', 'EE'),
    'tartu': ('Tartu', 'EE'), 'pärnu': ('Pärnu', 'EE'),
    'haapsalu': ('Haapsalu', 'EE'), 'rapla': ('Rapla', 'EE'),
    'viljandi': ('Viljandi', 'EE'), 'rakvere': ('Rakvere', 'EE'),
    'narva': ('Narva', 'EE'), 'võru': ('Võru', 'EE'),
    'kuusalu': ('Kuusalu', 'EE'), 'põltsamaa': ('Põltsamaa', 'EE'),
    'naissaar': ('Naissaar', 'EE'), 'alatskivi': ('Alatskivi', 'EE'),
    'tamsalu': ('Tamsalu', 'EE'), 'dublin': ('Dublin', 'IE'),
    'dublini': ('Dublin', 'IE'), 'cork': ('Cork', 'IE'),
    'heerlen': ('Heerlen', 'NL'), 'amsterdam': ('Amsterdam', 'NL'),
    'haag': ('The Hague', 'NL'), 'utrecht': ('Utrecht', 'NL'),
    'nijmegen': ('Nijmegen', 'NL'), 'tilburg': ('Tilburg', 'NL'),
    'cambridge': ('Cambridge', 'GB'), 'london': ('London', 'GB'),
    'leverkusen': ('Leverkusen', 'DE'),
}

VENUE_DEFAULTS = {
    'estonia kontserdisaal': ('Tallinn', 'EE'),
    'estonia kontserdisaali': ('Tallinn', 'EE'),
    'estonian concert hall': ('Tallinn', 'EE'),
    'toom church': ('Tallinn', 'EE'),
}

DATE_TIME_RE = re.compile(
    r'(?P<day>\d{1,2})\.(?P<month>\d{1,2})(?:\.(?P<year>\d{4}))?'
    r'(?:\s+kell\s+(?P<time>[0-2]?\d:[0-5]\d))?'
)
LEGACY_DATE_RE = re.compile(
    r'^(?:inc\.\s+)?(?:Sept\.?|(?P<month>[A-Za-z]+))\s*\.?' 
    r'\s+(?P<day>\d{1,2})$', re.IGNORECASE
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response


def parse_location(value):
    location = clean_text(value).strip(' ,')
    if not location:
        return None

    parts = [part.strip() for part in location.split(',') if part.strip()]
    country_code = None
    if parts:
        country_code = COUNTRIES.get(parts[-1].lower().rstrip('.'))
        if country_code:
            parts.pop()

    searchable = ' '.join(parts).lower()
    city = None
    default = VENUE_DEFAULTS.get(searchable)
    if default:
        city, hinted_country = default
        country_code = country_code or hinted_country
    for hint, resolved in CITY_HINTS.items():
        if city:
            break
        if re.search(rf'(?<!\w){re.escape(hint)}(?!\w)', searchable):
            city, hinted_country = resolved
            country_code = country_code or hinted_country
            break

    # The site's usual touring format is "venue, city, country".
    if not city and len(parts) >= 2:
        candidate = parts[-1]
        if not re.search(r'\b(?:saal|hall|kirik|church|cathedral|teater|theatre)\b', candidate, re.I):
            city = candidate

    if not city or not country_code:
        return None

    venue_parts = list(parts)
    if len(venue_parts) >= 2 and venue_parts[-1].casefold() == city.casefold():
        venue_parts.pop()
    venue = clean_text(', '.join(venue_parts))
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def parse_calendar_page(html, year):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('a.concert[href]'):
        title = clean_text(item.select_one('.concert__title'))
        url = urljoin(EVENTS_URL, item.get('href'))
        date_match = DATE_TIME_RE.search(clean_text(item.select_one('.date-time')))
        location = parse_location(item.select_one('p.mb0'))
        if not title or not date_match or not location:
            continue
        item_year = date_match.group('year') or year
        if not item_year:
            month_group = item.find_parent(class_='concert-month')
            heading = clean_text(month_group.find('h2')) if month_group else ''
            heading_year = re.search(r'\b(20\d{2})\b', heading)
            item_year = heading_year.group(1) if heading_year else None
        if not item_year:
            continue
        try:
            event_date = date(
                int(item_year),
                int(date_match.group('month')),
                int(date_match.group('day')),
            ).isoformat()
        except ValueError:
            continue
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': date_match.group('time'),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(session, url):
    soup = BeautifulSoup(fetch(session, url).text, 'html.parser')
    body = soup.select_one('article.post-type-concert .article-body')
    return clean_text(body) or None


def legacy_records(html, page_url):
    soup = BeautifulSoup(html, 'html.parser', from_encoding='windows-1252')
    records = []
    year = None
    for row in soup.select('tr'):
        cells = row.find_all('td', recursive=False)
        row_text = clean_text(row)
        if re.fullmatch(r'(?:19|20)\d{2}', row_text):
            year = int(row_text)
            continue
        if year is None or len(cells) < 2:
            continue

        date_match = LEGACY_DATE_RE.match(clean_text(cells[0]))
        event_text = clean_text(cells[1])
        if not date_match or not re.match(r'^Concert\s+in\s+', event_text, re.I):
            continue
        month_name = date_match.group('month') or 'September'
        month = MONTHS.get(month_name.lower())
        location_text = re.sub(r'^Concert\s+in\s+', '', event_text, flags=re.I)
        location = parse_location(location_text)
        if not month or not location:
            continue
        try:
            event_date = date(year, month, int(date_match.group('day'))).isoformat()
        except ValueError:
            continue
        venue, city, country_code = location
        programme = next(
            (a for a in cells[1].select('a[href]') if 'kava.html#' in a.get('href', '')),
            None,
        )
        url = urljoin(page_url, programme.get('href')) if programme else page_url
        description = clean_text(cells[2]) if len(cells) > 2 else ''
        records.append({
            'title': f'{SOURCE}: {venue}',
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class EpccEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='epcc_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        landing = BeautifulSoup(fetch(session, EVENTS_URL).text, 'html.parser')
        years = {
            int(option.get_text(strip=True))
            for option in landing.select('select option')
            if re.fullmatch(r'20\d{2}', option.get_text(strip=True))
        }

        records = []
        records.extend(parse_calendar_page(str(landing), None))
        for year in sorted(years):
            url = f'{EVENTS_URL}?y={year}'
            try:
                records.extend(parse_calendar_page(fetch(session, url).text, year))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch EPCC archive year',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        for url in LEGACY_URLS:
            try:
                response = fetch(session, url)
                records.extend(legacy_records(response.content, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch EPCC legacy archive',
                    event='crawler_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        detail_urls = {record['url'] for record in records if '/kontserdid/' in record['url']}
        descriptions = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(detail_description, session, url): url
                for url in detail_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch EPCC concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url'], record['description'])

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    EpccEeCrawler().run()


if __name__ == '__main__':
    main()
