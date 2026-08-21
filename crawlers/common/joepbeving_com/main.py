import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.joepbeving.com/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
SOURCE = 'Joep Beving'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The calendar spells out country names rather than publishing ISO codes.
# Include the source's current tour countries and common touring destinations.
COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'brazil': 'BR',
    'canada': 'CA',
    'china': 'CN',
    'czech republic': 'CZ',
    'czechia': 'CZ',
    'denmark': 'DK',
    'estonia': 'EE',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'greece': 'GR',
    'hungary': 'HU',
    'iceland': 'IS',
    'ireland': 'IE',
    'italy': 'IT',
    'japan': 'JP',
    'latvia': 'LV',
    'lithuania': 'LT',
    'luxembourg': 'LU',
    'mexico': 'MX',
    'netherlands': 'NL',
    'new zealand': 'NZ',
    'norway': 'NO',
    'poland': 'PL',
    'portugal': 'PT',
    'singapore': 'SG',
    'slovakia': 'SK',
    'slovenia': 'SI',
    'south africa': 'ZA',
    'south korea': 'KR',
    'spain': 'ES',
    'sweden': 'SE',
    'switzerland': 'CH',
    'taiwan': 'TW',
    'turkey': 'TR',
    'united arab emirates': 'AE',
    'united kingdom': 'GB',
    'uk': 'GB',
    'united states': 'US',
    'usa': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_article(article):
    title = clean_text(article.select_one('.concert__title'))
    venue = clean_text(article.select_one('.concert__location-title'))
    location_parts = [clean_text(part).rstrip(',').strip() for part in article.select(
        '.concert__location span'
    )]
    location_parts = [part for part in location_parts if part]
    if len(location_parts) < 2:
        return None

    city = location_parts[0]
    country_code = COUNTRY_CODES.get(location_parts[-1].casefold())
    time_element = article.select_one('time[datetime]')
    ticket_link = article.select_one('a.concert__link[href]')
    if not title or not venue or not city or not country_code or not time_element or not ticket_link:
        return None

    raw_date = time_element.get('datetime', '')[:10]
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return None

    url = ticket_link.get('href', '').strip()
    if not url:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JoepbevingComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='joepbeving_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            response.encoding = 'utf-8'
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Joep Beving concerts',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for article in soup.select('main article.concert'):
            record = parse_article(article)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue'], record['url']),
        )


def main():
    JoepbevingComCrawler().run()


if __name__ == '__main__':
    main()
