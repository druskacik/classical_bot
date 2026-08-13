import re
from datetime import datetime
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroponchielli.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario-eventi/')
SOURCE = 'Teatro Amilcare Ponchielli'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
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


def calendar_months(soup):
    months = []
    for option in soup.select('#month-filter option[value]'):
        value = option.get('value', '').strip()
        if re.fullmatch(r'\d{4}-\d{2}', value) and value not in months:
            months.append(value)
    return months


def detail_urls(soup):
    urls = []
    for link in soup.select('article.bs-show-item h2.bs-show-item__title a[href]'):
        url = urljoin(SOURCE_URL, link['href'])
        if url.startswith(SOURCE_URL) and url not in urls:
            urls.append(url)
    return urls


def parse_date(value):
    try:
        return datetime.strptime(value, '%d.%m.%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', value or '')
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_detail(soup, url):
    title = clean_text(soup.select_one('main h1.entry-title'))
    page_location = clean_text(soup.select_one('.event-primary-info p'))
    description = clean_text(soup.select_one('.entry-content')) or None
    if not title or not page_location:
        return []

    records = []
    for occurrence in soup.select('.event-replicas .event-replica'):
        event_date = parse_date(clean_text(occurrence.select_one('.event-date')))
        time_from = parse_time(clean_text(occurrence.select_one('.event-hour')))
        venue = clean_text(occurrence.select_one('.event-location'))
        if not event_date or not venue:
            continue
        city = (
            'Cremona'
            if 'teatro amilcare ponchielli' in venue.casefold()
            else page_location.title()
        )
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class TeatroPonchielliItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroponchielli_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            first_soup = get_soup(session, CALENDAR_URL)
            months = calendar_months(first_soup)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Teatro Ponchielli calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        urls = detail_urls(first_soup)
        for month in months:
            calendar_url = f'{CALENDAR_URL}?{urlencode({"month": month, "tap-rassegna": "all"})}'
            try:
                for url in detail_urls(get_soup(session, calendar_url)):
                    if url not in urls:
                        urls.append(url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro Ponchielli calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=calendar_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        for url in urls:
            try:
                records.extend(parse_detail(get_soup(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro Ponchielli event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    TeatroPonchielliItCrawler().run()


if __name__ == '__main__':
    main()
