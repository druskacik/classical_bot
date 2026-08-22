import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ralphvanraat.com/'
PERFORMANCES_URL = f'{SOURCE_URL}performances/'
SOURCE = 'Ralph van Raat'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The calendar belongs to a Dutch pianist and omits the country for most Dutch
# engagements. Touring cities are resolved explicitly rather than inheriting NL.
FOREIGN_COUNTRIES = {
    'Alicante (Spain)': 'ES',
    'Antwerp (Belgium)': 'BE',
    'Antwerpen': 'BE',
    'Athens': 'GR',
    'Asnières-sur-Oise': 'FR',
    'Bari': 'IT',
    'Beijing, China': 'CN',
    'Brugge': 'BE',
    'Brussels': 'BE',
    'Ghent (Belgium)': 'BE',
    'Köln': 'DE',
    'London': 'GB',
    'Los Angeles': 'US',
    'Malmö': 'SE',
    'Manchester': 'GB',
    'Mendocino': 'US',
    'Neuss (Germany)': 'DE',
    'Oakland': 'US',
    'Paris': 'FR',
    'Shenzhen, China': 'CN',
    'Strasbourg': 'FR',
    'Tallinn, Estland': 'EE',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(day, month_year):
    for date_format in ('%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(f'{day} {month_year}', date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*([ap])m', clean_text(value), re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def archive_record(article):
    link = article.select_one('a.event-link[href]')
    title = clean_text(article.select_one('.sr_it-event-title h1'))
    day = clean_text(article.select_one('.sr-it-date-day'))
    month_year = clean_text(article.select_one('.sr-it-date-years'))
    event_date = parse_date(day, month_year)
    venue = clean_text(article.select_one('.eventlist-venue')).lstrip('@').strip()
    city = clean_text(article.select_one('.eventlist-city')).lstrip('|').strip()
    url = link.get('href', '').strip() if link else ''
    if not title or not event_date or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': FOREIGN_COUNTRIES.get(city, 'NL'),
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def enrich_record(record):
    response = requests.get(record['url'], headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    article = soup.select_one('article.event')
    if not article:
        return record

    for row in article.select('table tr'):
        value = clean_text(row)
        parsed = parse_time(value)
        if parsed:
            record['time_from'] = parsed
            break

    paragraphs = []
    for paragraph in article.select('p'):
        text = clean_text(paragraph)
        if text and text not in paragraphs:
            paragraphs.append(text)
    record['description'] = '\n\n'.join(paragraphs) or None
    return record


class RalphvanraatComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ralphvanraat_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        response = requests.get(PERFORMANCES_URL, headers=HEADERS, timeout=90)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for article in soup.select('article.event'):
            record = archive_record(article)
            if record:
                records.append(record)

        enriched = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(enrich_record, record): record for record in records}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    enriched.append(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Ralph van Raat concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    enriched.append(record)

        return sorted(
            enriched,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    RalphvanraatComCrawler().run()


if __name__ == '__main__':
    main()
