import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://monteverdi.co.uk/'
CALENDAR_URL = urljoin(SOURCE_URL, 'whats-on')
ARCHIVE_URL = urljoin(SOURCE_URL, 'recent-projects/recent')
SOURCE = 'Monteverdi Choir & Orchestras'

HEADERS = {
    # Cloudflare serves the public, indexable concert pages to search crawlers.
    'User-Agent': 'Googlebot',
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s+(\d{1,2})\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(20\d{2})(?:\s*,?\s*(.*))?$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)

COUNTRY_CODES = {
    'argentina': 'AR', 'austria': 'AT', 'belgium': 'BE', 'brazil': 'BR',
    'canada': 'CA', 'china': 'CN', 'colombia': 'CO', 'croatia': 'HR',
    'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK', 'finland': 'FI',
    'france': 'FR', 'germany': 'DE', 'hungary': 'HU', 'italy': 'IT',
    'japan': 'JP', 'luxembourg': 'LU', 'netherlands': 'NL', 'norway': 'NO',
    'peru': 'PE', 'poland': 'PL', 'portugal': 'PT', 'romania': 'RO',
    'slovakia': 'SK', 'slovenia': 'SI', 'south korea': 'KR', 'spain': 'ES',
    'sweden': 'SE', 'switzerland': 'CH', 'uk': 'GB', 'united kingdom': 'GB',
    'uruguay': 'UY', 'usa': 'US', 'united states': 'US',
}

# Older project pages often omit the country but identify an unambiguous tour city.
CITY_COUNTRIES = {
    'amsterdam': 'NL', 'athens': 'GR', 'atlanta': 'US', 'barcelona': 'ES',
    'basel': 'CH', 'berkeley': 'US', 'bergen': 'NO', 'berlin': 'DE',
    'birmingham': 'GB', 'bologna': 'IT', 'boston': 'US', 'brussels': 'BE',
    'budapest': 'HU', 'buenos aires': 'AR', 'carmel': 'US', 'cartagena': 'CO',
    'chicago': 'US', 'cologne': 'DE', 'dresden': 'DE', 'edinburgh': 'GB',
    'florence': 'IT', 'gateshead': 'GB', 'geneva': 'CH', 'glasgow': 'GB',
    'graz': 'AT', 'hamburg': 'DE', 'innsbruck': 'AT', 'kansas city': 'US',
    'leipzig': 'DE', 'lima': 'PE', 'lisbon': 'PT', 'liverpool': 'GB',
    'london': 'GB', 'los angeles': 'US', 'lucerne': 'CH', 'madrid': 'ES',
    'manchester': 'GB', 'melk': 'AT', 'milan': 'IT', 'monreale': 'IT',
    'montevideo': 'UY', 'munich': 'DE', 'new york': 'US', 'new york city': 'US',
    'ottawa': 'CA', 'oxford': 'GB', 'paris': 'FR', 'princeton': 'US',
    'prague': 'CZ', 'rimini': 'IT', 'rome': 'IT', 'salzburg': 'AT',
    'são paulo': 'BR', 'sao paulo': 'BR', 'seoul': 'KR', 'stuttgart': 'DE',
    'turin': 'IT', 'venice': 'IT', 'versailles': 'FR', 'vienna': 'AT',
    'warsaw': 'PL', 'weimar': 'DE', 'wrocław': 'PL', 'zurich': 'CH',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def country_code(value, city):
    normalized = clean_text(value).strip(' ,').lower()
    if normalized in COUNTRY_CODES:
        return COUNTRY_CODES[normalized]
    city_key = clean_text(city).split(',')[0].strip().lower()
    return CITY_COUNTRIES.get(city_key)


def page_description(soup):
    content = soup.select_one('section.content')
    if not content:
        return None
    copy = BeautifulSoup(str(content), 'html.parser')
    for node in copy.select('.hero, header.pageTitle, .listing, iframe, script, style'):
        node.decompose()
    text = clean_text(copy)
    return text or None


def detail_data(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('section.content h1'))
    return title, page_description(soup), soup


def parse_calendar(session):
    soup = get_soup(session, CALENDAR_URL)
    records = []
    details = {}
    rows = soup.select('tr.eventType.concert')
    urls = list(dict.fromkeys(urljoin(SOURCE_URL, row.select_one('strong a')['href']) for row in rows))
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(detail_data, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()[:2]
            except requests.RequestException as error:
                log_message('Failed to scrape Monteverdi project detail', event='crawler_item_failed',
                            level='warning', url=url, error_type=type(error).__name__,
                            error_message=str(error))

    for row in rows:
        cells = row.find_all('td', recursive=False)
        if len(cells) < 5:
            continue
        date_cell, venue_cell, city_cell, country_cell, title_cell = cells[:5]
        month_heading = row.find_previous('th')
        month_text = clean_text(month_heading)
        year_match = re.search(r'(20\d{2})', month_text)
        day_match = re.search(r'\b(\d{1,2})\b', clean_text(date_cell))
        month_match = re.search('|'.join(datetime(2000, month, 1).strftime('%B') for month in range(1, 13)), month_text)
        link = title_cell.select_one('strong a')
        venue, city = clean_text(venue_cell), clean_text(city_cell)
        code = country_code(country_cell, city)
        if not (year_match and day_match and month_match and link and venue and city and code):
            continue
        try:
            event_date = datetime.strptime(
                f'{day_match.group(1)} {month_match.group(0)} {year_match.group(1)}', '%d %B %Y'
            ).date().isoformat()
        except ValueError:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        detail_title, description = details.get(url, ('', None))
        records.append({
            'title': detail_title or clean_text(link), 'date': event_date, 'url': url,
            'time_from': parse_time(clean_text(date_cell)), 'venue': venue, 'city': city,
            'country_code': code, 'description': description,
            'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return records


def archive_urls(session):
    soup = get_soup(session, ARCHIVE_URL)
    return list(dict.fromkeys(
        urljoin(SOURCE_URL, link['href']) for link in soup.select('a[href*="/recent-projects/"]')
        if '/archive/' not in link['href'] and not link['href'].rstrip('/').endswith('/recent')
    ))


def parse_archive_page(session, url):
    title, description, soup = detail_data(session, url)
    if not title:
        return []
    records = []
    for paragraph in soup.select('section.content p'):
        lines = [line for line in clean_text(paragraph).splitlines() if line]
        if len(lines) < 3:
            continue
        match = DATE_RE.match(lines[0])
        if not match:
            continue
        try:
            event_date = datetime.strptime(
                f'{match.group(1)} {match.group(2)} {match.group(3)}', '%d %B %Y'
            ).date().isoformat()
        except ValueError:
            continue
        location = [line.strip(' ,') for line in lines[1:] if not re.match(
            r'^(?:book|more info|find out|sold out|tickets?)\b', line, re.IGNORECASE
        )]
        if len(location) < 2:
            continue
        explicit_code = country_code(location[-1], location[-2])
        if clean_text(location[-1]).lower() in COUNTRY_CODES:
            country, city, venue_parts = explicit_code, location[-2], location[:-2]
        else:
            country, city, venue_parts = country_code('', location[-1]), location[-1], location[:-1]
        venue = ', '.join(venue_parts)
        if not (country and city and venue):
            continue
        records.append({
            'title': title, 'date': event_date, 'url': url,
            'time_from': parse_time(match.group(4)), 'venue': venue, 'city': city,
            'country_code': country, 'description': description,
            'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return records


class MonteverdiCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='monteverdi_co_uk', source=SOURCE, source_url=SOURCE_URL,
        country_code='GB', upload_target='classical',
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = parse_calendar(session)
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(parse_archive_page, session, url): url for url in archive_urls(session)}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message('Failed to scrape Monteverdi archive project', event='crawler_item_failed',
                                level='warning', url=url, error_type=type(error).__name__,
                                error_message=str(error))
        unique = {(record['title'], record['date'], record['time_from'], record['venue'], record['city']): record
                  for record in records}
        return sorted(unique.values(), key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    MonteverdiCoUkCrawler().run()


if __name__ == '__main__':
    main()
