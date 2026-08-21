import re
from datetime import date

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jakubjozeforlinski.com/'
SOURCE = 'Jakub Józef Orliński'
LIVE_URL = 'https://www.jakubjozeforlinski.com/live'

HEADERS = {
    'Accept-Language': 'en-US,en;q=0.9',
}

PAST_PARAMS = {
    'field_tour_date_value': '2',
    'field_tour_date_end_value_op': '<',
    'field_tour_date_end_value[value]': 'now',
    'sort_by': 'sort_filter',
    'sort_order': 'DESC',
}

MONTHS = {
    'jan': 1,
    'january': 1,
    'feb': 2,
    'february': 2,
    'mar': 3,
    'march': 3,
    'apr': 4,
    'april': 4,
    'may': 5,
    'jun': 6,
    'june': 6,
    'jul': 7,
    'july': 7,
    'aug': 8,
    'august': 8,
    'sep': 9,
    'sept': 9,
    'september': 9,
    'oct': 10,
    'october': 10,
    'nov': 11,
    'november': 11,
    'dec': 12,
    'december': 12,
}

COUNTRY_CODES = {
    'Andorra': 'AD',
    'Argentina': 'AR',
    'Austria': 'AT',
    'Belgium': 'BE',
    'Brazil': 'BR',
    'Bulgaria': 'BG',
    'Canada': 'CA',
    'Chile': 'CL',
    'China': 'CN',
    'Czech Republic': 'CZ',
    'Finland': 'FI',
    'France': 'FR',
    'Germany': 'DE',
    'Greece': 'GR',
    'Hungary': 'HU',
    'Italy': 'IT',
    'Japan': 'JP',
    'Korea, South': 'KR',
    'Latvia': 'LV',
    'Lithuania': 'LT',
    'Luxembourg': 'LU',
    'Mexico': 'MX',
    'Netherlands': 'NL',
    'Poland': 'PL',
    'Singapore': 'SG',
    'Spain': 'ES',
    'Sweden': 'SE',
    'Switzerland': 'CH',
    'Taiwan': 'TW',
    'Turkey': 'TR',
    'United Kingdom': 'GB',
    'United States': 'US',
    'Uruguay': 'UY',
}


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_date(value):
    match = re.fullmatch(r'([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})', value.strip())
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_event_dates(wrapper):
    date_nodes = wrapper.select('.dateWrap > div')
    start = parse_date(clean_text(date_nodes[0] if date_nodes else None))
    if start is None:
        return []

    values = [start]
    multiple = clean_text(date_nodes[2] if len(date_nodes) >= 3 else None)
    if not multiple:
        return values

    year_match = re.search(r'\b(20\d{2})\b', multiple)
    month_match = re.search(r'\b([A-Za-z]+)\b', multiple)
    if not year_match or not month_match:
        return values
    month = MONTHS.get(month_match.group(1).lower())
    if month is None:
        return values

    prefix = multiple[:year_match.start()]
    day_values = re.findall(r'\b\d{1,2}\b', re.sub(r'[A-Za-z]+', ' ', prefix))
    for day_value in day_values:
        try:
            event_date = date(int(year_match.group(1)), month, int(day_value)).isoformat()
        except ValueError:
            continue
        if event_date not in values:
            values.append(event_date)
    return sorted(values)


def parse_wrapper(wrapper):
    title = clean_text(wrapper.select_one('.tourTitle'))
    venue = clean_text(wrapper.select_one('.locationOfTour'))
    location = clean_text(wrapper.select_one('.city_country, .filtercountry'))
    link = wrapper.select_one('.ticketLink a[href]')
    country_name = wrapper.get('data-c', '').strip()
    country_code = COUNTRY_CODES.get(country_name)

    city = ''
    suffix = f', {country_name}'
    if country_name and location.endswith(suffix):
        city = location[:-len(suffix)].strip()

    if not title or not venue or not city or not country_code or link is None:
        return []

    url = link.get('href', '').strip()
    if not url:
        return []

    return [
        {
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
        for event_date in parse_event_dates(wrapper)
    ]


class JakubJozefOrlinskiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jakubjozeforlinski_com',
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
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        responses = []
        try:
            for params in ({}, PAST_PARAMS):
                response = session.get(
                    LIVE_URL,
                    params=params,
                    impersonate='chrome',
                    timeout=45,
                )
                response.raise_for_status()
                responses.append(response)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Jakub Józef Orliński live dates',
                event='crawler_fetch_failed',
                level='error',
                url=LIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for response in responses:
            soup = BeautifulSoup(response.text, 'html.parser')
            for wrapper in soup.select('.eventWrapper'):
                records.extend(parse_wrapper(wrapper))

        unique_records = {
            (record['title'], record['date'], record['venue'], record['city']): record
            for record in records
        }
        return sorted(
            unique_records.values(),
            key=lambda record: (
                record['date'], record['title'], record['venue'], record['city']
            ),
        )


def main():
    JakubJozefOrlinskiComCrawler().run()


if __name__ == '__main__':
    main()
