import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://seongjin-cho.com/'
PERFORMANCES_URL = f'{SOURCE_URL}performances/'
SOURCE = 'Seong-Jin Cho'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The first-party calendar is a worldwide touring schedule but does not expose
# country fields.  Keep this explicit so an unfamiliar city is skipped rather
# than assigned an unsafe home-country default.
CITY_COUNTRIES = {
    'Amsterdam': 'NL',
    'Antwerp': 'BE',
    'Barcelona': 'ES',
    'Bedford': 'GB',
    'Berlin': 'DE',
    'Boston': 'US',
    'Brussels': 'BE',
    'Frankfurt': 'DE',
    'Hamburg': 'DE',
    'Hannover': 'DE',
    'Leicester': 'GB',
    'Lisbon': 'PT',
    'London': 'GB',
    'Lucerne': 'CH',
    'Madrid': 'ES',
    'Munich': 'DE',
    'Paris': 'FR',
    'Seoul': 'KR',
    'Stockholm': 'SE',
    'Wien': 'AT',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d %y').date().isoformat()
    except ValueError:
        return None


def parse_city(value):
    city = re.sub(r'^(?:performance|recital)\s*:\s*', '', clean_text(value), flags=re.I)
    return city.strip()


def parse_event(article):
    heading = article.select_one('.event-item-title')
    city = parse_city(heading)
    country_code = CITY_COUNTRIES.get(city)
    event_date = parse_date(article.select_one('time'))
    url = clean_text(article.get('data-href'))

    locations = [clean_text(item) for item in article.select('.event-item-location')]
    locations = [item for item in locations if item]
    venue = locations[-1] if locations else ''

    repertoire = clean_text(article.select_one('.event-item-description'))
    description_parts = locations[:-1]
    if repertoire:
        description_parts.append(repertoire)
    description = '\n\n'.join(description_parts) or None

    if not city or not country_code or not event_date or not url or not venue:
        log_message(
            'Skipped incomplete Seong-Jin Cho performance',
            event='crawler_item_skipped',
            level='warning',
            url=url or PERFORMANCES_URL,
            error_type='IncompleteEventData',
            error_message='Required date, URL, venue, city, or country code is missing',
        )
        return None

    title = clean_text(heading)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


class SeongjinChoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='seongjin_cho_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(PERFORMANCES_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('article.event-item')
        if not articles:
            raise ValueError('No performance entries found on the first-party calendar')

        records = []
        for article in articles:
            record = parse_event(article)
            if record:
                records.append(record)
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    SeongjinChoComCrawler().run()


if __name__ == '__main__':
    main()
