import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lesleygarrett.co.uk/'
SOURCE = 'Lesley Garrett'
EVENTS_URL = f'{SOURCE_URL}events/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

UK_CITIES = {
    'belfast', 'birmingham', 'bristol', 'cambridge', 'cardiff', 'doncaster',
    'edinburgh', 'glasgow', 'leeds', 'liverpool', 'london', 'manchester',
    'newcastle', 'nottingham', 'oxford', 'sheffield', 'york',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        raw = html.unescape(str(value))
        text = (
            BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
            if '<' in raw
            else raw
        )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    value = re.sub(r'^\w{3,9}\s+', '', value)
    value = re.sub(r'(\d{1,2})(?:st|nd|rd|th)', r'\1', value, flags=re.I)
    for pattern in ('%d %B, %Y', '%d %B %Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def normalized_title(value):
    return re.sub(r'\W+', ' ', clean_text(value), flags=re.UNICODE).casefold().strip()


def parse_location(description):
    """Extract only locations explicitly phrased as a venue in a city."""
    text = clean_text(description)
    match = re.search(
        r'\bat\s+(?:the\s+)?(?:lovely\s+|beautiful\s+|wonderful\s+)?'
        r'([^.!?\n,]{3,100}?)\s+in\s+([A-Z][A-Za-zÀ-ÖØ-öø-ÿ .\'-]{1,50})'
        r'(?=[,.!?]|$)',
        text,
    )
    if not match:
        return None

    venue = clean_text(match.group(1)).strip(' ,')
    city = clean_text(match.group(2)).strip(' ,')
    city = re.sub(r'\s+(?:on|from|between)\s+.*$', '', city, flags=re.I)
    city = city.strip(' ,.!?')
    if not venue or not city or venue.casefold() == city.casefold():
        return None

    lowered = city.casefold()
    if lowered in UK_CITIES:
        country_code = 'GB'
    else:
        # The artist tours internationally; an unknown city must not inherit her
        # home country without explicit evidence.
        country_code = None
    return venue, city, country_code


def listing_items(document):
    soup = BeautifulSoup(document, 'html.parser')
    items = []
    for article in soup.select('article.ae-post-list-item'):
        title = clean_text(article.select_one('.ae-element-post-title'))
        date_value = None
        for field in article.select('.ae-acf-content-wrapper'):
            candidate = parse_date(field)
            if candidate:
                date_value = candidate
                break
        if title and date_value:
            items.append((title, date_value))
    return items


class LesleyGarrettCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lesleygarrett_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        listing_response = session.get(EVENTS_URL, timeout=45)
        listing_response.raise_for_status()
        dated_items = listing_items(listing_response.text)

        api_response = session.get(
            EVENTS_API_URL,
            params={'per_page': 100, 'page': 1, 'orderby': 'date', 'order': 'desc'},
            timeout=45,
        )
        api_response.raise_for_status()
        api_items = {
            normalized_title(item.get('title', {}).get('rendered')): item
            for item in api_response.json()
        }

        records = []
        for title, event_date in dated_items:
            item = api_items.get(normalized_title(title))
            if not item:
                log_message(
                    'Skipped Lesley Garrett event without matching detail page',
                    event='crawler_item_skipped',
                    level='warning',
                    url=EVENTS_URL,
                    error_type='MissingEventDetail',
                    error_message='The dated listing could not be matched to the events API',
                )
                continue
            description = clean_text((item.get('content') or {}).get('rendered'))
            location = parse_location(description)
            if not location or not location[2]:
                log_message(
                    'Skipped Lesley Garrett event without a defensible location',
                    event='crawler_item_skipped',
                    level='warning',
                    url=item.get('link') or EVENTS_URL,
                    error_type='IncompleteEventData',
                    error_message='Venue, city, or country could not be extracted',
                )
                continue
            venue, city, country_code = location
            records.append({
                'title': title,
                'date': event_date,
                'url': clean_text(item.get('link')),
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(records, key=lambda record: (record['date'], record['title']))


def main():
    LesleyGarrettCoUkCrawler().run()


if __name__ == '__main__':
    main()
