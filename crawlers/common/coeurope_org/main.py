import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://coeurope.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'coe-concerts/coe-concerts-calendar/')
SOURCE = 'Chamber Orchestra of Europe'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'canada': 'CA',
    'china': 'CN', 'croatia': 'HR', 'cyprus': 'CY', 'czech republic': 'CZ',
    'denmark': 'DK', 'estonia': 'EE', 'finland': 'FI', 'france': 'FR',
    'germany': 'DE', 'greece': 'GR', 'hong kong': 'HK', 'hungary': 'HU',
    'ireland': 'IE', 'israel': 'IL', 'italy': 'IT', 'japan': 'JP',
    'luxembourg': 'LU', 'netherlands': 'NL', 'new zealand': 'NZ',
    'norway': 'NO', 'poland': 'PL', 'portugal': 'PT', 'romania': 'RO',
    'singapore': 'SG', 'slovakia': 'SK', 'slovenia': 'SI',
    'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'the netherlands': 'NL', 'uae': 'AE', 'uk': 'GB',
    'united kingdom': 'GB', 'usa': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.text


def parse_location(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if len(parts) < 3:
        return None
    country_code = COUNTRY_CODES.get(parts[-1].lower())
    venue = ', '.join(parts[:-2]).strip()
    city = parts[-2]
    if not country_code or not venue or not city:
        return None
    return venue, city, country_code


def parse_record(article):
    # Early archive entries predate project titles; the source identifies all
    # such rows as Chamber Orchestra of Europe performances.
    title = clean_text(article.select_one('.c-list-concert__title')) or SOURCE
    day_text = clean_text(article.select_one('.c-list-concert__day'))
    date_block = article.select_one('.c-list-concert__day + div')
    date_text = clean_text(date_block)
    location = parse_location(clean_text(article.select_one('.c-list-concert__address')))
    detail_link = article.select_one('a[href*="/coe-projects-list/"]')
    url = urljoin(SOURCE_URL, detail_link.get('href')) if detail_link else ''
    if not url and article.get('id'):
        # Older occurrences have no project page, but their WordPress post ID
        # provides a stable first-party anchor on the complete calendar.
        url = f'{CALENDAR_URL}#{article["id"]}'

    month_match = re.search(r'([A-Za-z]+),\s*(\d{4})', date_text)
    if not title or not day_text.isdigit() or not month_match or not location or not url:
        return None
    try:
        event_date = datetime.strptime(
            f'{day_text} {month_match.group(1)} {month_match.group(2)}', '%d %B %Y'
        ).date().isoformat()
    except ValueError:
        return None

    time_match = re.search(r'\b(\d{1,2})[.:](\d{2})\b', date_text)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23 and int(time_match.group(2)) <= 59:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_node = article.select_one('.columns.small-10.medium-6 .text')
    description = clean_text(description_node)
    if description_node:
        for link in description_node.select('a'):
            link.extract()
        description = clean_text(description_node) or None
    venue, city, country_code = location
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': country_code,
        'description': description, 'source_url': SOURCE_URL, 'source': SOURCE,
    }


def parse_year(html):
    soup = BeautifulSoup(html, 'html.parser')
    return [record for article in soup.select('article.c-list-concert')
            if (record := parse_record(article))]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_html = get_html(session, CALENDAR_URL)
    soup = BeautifulSoup(first_html, 'html.parser')
    years = sorted({option.get('value') for option in soup.select('[name="filter_year"] option')
                    if (option.get('value') or '').isdigit()})
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_html, session, CALENDAR_URL,
                            {'filter': '1', 'filter_year': year}): year
            for year in years
        }
        for future in as_completed(futures):
            year = futures[future]
            try:
                records.extend(parse_year(future.result()))
            except requests.RequestException as error:
                log_message('Failed to scrape concert year', event='crawler_page_failed',
                            level='warning', url=f'{CALENDAR_URL}?filter=1&filter_year={year}',
                            error_type=type(error).__name__, error_message=str(error))
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['url']))


class CoeuropeOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='coeurope_org', source=SOURCE, source_url=SOURCE_URL,
        country_code=None, upload_target='classical',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city',
                 'country_code', 'description', 'source_url', 'source'],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CoeuropeOrgCrawler().run()


if __name__ == '__main__':
    main()
