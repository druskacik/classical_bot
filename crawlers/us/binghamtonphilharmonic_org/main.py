import re
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://binghamtonphilharmonic.org/'
LISTING_URL = urljoin(SOURCE_URL, 'calendar-list')
SOURCE = 'Binghamton Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
TURBO_HEADERS = {
    'Accept': 'text/vnd.turbo-stream.html, text/html, application/xhtml+xml',
    'Referer': LISTING_URL,
}


def clean_text(element, separator=' '):
    if element is None:
        return ''
    text = element.get_text(separator, strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def response_soup(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    template = soup.select_one('turbo-stream template')
    if template is not None:
        return BeautifulSoup(template.decode_contents(), 'html.parser')
    return soup


def parse_date(value, default_year=None):
    value = clean_text(value)
    patterns = ('%A, %B %d, %Y', '%A, %B %d')
    for pattern in patterns:
        try:
            parsed = datetime.strptime(value, pattern)
            year = parsed.year if '%Y' in pattern else (default_year or date.today().year)
            return parsed.date().replace(year=year).isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    value = clean_text(value).upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_location(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if len(parts) < 3:
        return None
    if re.fullmatch(r'[A-Z]{2}(?:\s+\d{5})?', parts[-1]):
        city = parts[-2]
    else:
        city = re.sub(r'\s+[A-Z]{2}(?:\s+\d{5})?$', '', parts[-1]).strip()
    venue = parts[0]
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city


def parse_article(article, default_year=None):
    title_link = article.select_one('.event-title a[href*="/event/"]')
    date_node = article.select_one('.date-long time.from .date')
    location = parse_location(article.select_one('.event-location'))
    title = clean_text(title_link)
    event_date = parse_date(date_node, default_year)
    if not title_link or not title or not event_date or not location:
        return None

    venue, city = location
    notes = article.select_one('.event-notes')
    description = clean_text(notes, separator='\n') or None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, title_link['href']),
        'time_from': parse_time(article.select_one('.date-long time.from .time')),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def page_number(href, parameter):
    values = parse_qs(urlparse(href).query).get(parameter, [])
    try:
        return int(values[0])
    except (IndexError, ValueError):
        return None


class BinghamtonPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='binghamtonphilharmonic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(LISTING_URL, timeout=45)
            response.raise_for_status()
            first_soup = response_soup(response)

            page_urls = {LISTING_URL}
            for parameter in ('calendar_page', 'calendar_page_prev'):
                seed_link = first_soup.select_one(f'a[href*="{parameter}="]')
                if seed_link is None:
                    continue
                seed_url = urljoin(LISTING_URL, seed_link['href'])
                seed_response = session.get(seed_url, headers=TURBO_HEADERS, timeout=45)
                seed_response.raise_for_status()
                seed_soup = response_soup(seed_response)
                numbered = [(page_number(seed_url, parameter) or 1, seed_url)]
                for link in seed_soup.select(f'a[href*="{parameter}="]'):
                    number = page_number(link.get('href', ''), parameter)
                    if number:
                        numbered.append((number, urljoin(LISTING_URL, link['href'])))
                last_page, sample_url = max(numbered)
                first_page = 2 if parameter == 'calendar_page' else 1
                for number in range(first_page, last_page + 1):
                    page_urls.add(re.sub(
                        rf'{parameter}=\d+', f'{parameter}={number}', sample_url
                    ))

            records = []
            for url in sorted(page_urls):
                if url == LISTING_URL:
                    soup = first_soup
                else:
                    page_response = session.get(url, headers=TURBO_HEADERS, timeout=45)
                    page_response.raise_for_status()
                    soup = response_soup(page_response)
                for article in soup.select('article.list-style'):
                    record = parse_article(article, default_year=date.today().year)
                    if record:
                        records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Binghamton Philharmonic calendar',
                event='crawler_fetch_failed',
                level='error',
                url=LISTING_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    BinghamtonPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
