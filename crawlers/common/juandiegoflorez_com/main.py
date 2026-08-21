import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.juandiegoflorez.com/'
SCHEDULE_URL = f'{SOURCE_URL}schedule'
SOURCE = 'Juan Diego Flórez'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'Argentina': 'AR',
    'Australia': 'AU',
    'Austria': 'AT',
    'Belgium': 'BE',
    'Brasil': 'BR',
    'Brazil': 'BR',
    'Canada': 'CA',
    'Chile': 'CL',
    'China': 'CN',
    'Colombia': 'CO',
    'Czech Republic': 'CZ',
    'Czechia': 'CZ',
    'Ecuador': 'EC',
    'France': 'FR',
    'Germany': 'DE',
    'Hong Kong': 'HK',
    'Hungary': 'HU',
    'Italy': 'IT',
    'Japan': 'JP',
    'Mexico': 'MX',
    'Monaco': 'MC',
    'Netherlands': 'NL',
    'Peru': 'PE',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Russia': 'RU',
    'Singapore': 'SG',
    'South Korea': 'KR',
    'Spain': 'ES',
    'Switzerland': 'CH',
    'United Arab Emirates': 'AE',
    'United Kingdom': 'GB',
    'United States': 'US',
    'USA': 'US',
}

MONTHS = {name: number for number, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June',
     'July', 'August', 'September', 'October', 'November', 'December'),
    start=1,
)}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_dates(value):
    """Expand strings such as 'February 12, 19, March 2, 2027'."""
    text = clean_text(value)
    year_match = re.search(r'\b(\d{4})\b', text)
    if not year_match:
        return []
    year = int(year_match.group(1))
    current_month = None
    parsed = []
    without_year = re.sub(r',?\s*\b\d{4}\b\s*$', '', text)
    for part in without_year.split(','):
        match = re.fullmatch(r'\s*(?:(\w+)\s+)?(\d{1,2})\s*', part)
        if not match:
            return []
        if match.group(1):
            current_month = MONTHS.get(match.group(1))
        if current_month is None:
            return []
        try:
            parsed.append(date(year, current_month, int(match.group(2))).isoformat())
        except ValueError:
            return []
    return parsed


def parse_event(article):
    title_node = article.select_one('.grlt_title a')
    date_node = article.select_one('.grlt_beforetitle')
    city_node = article.select_one('.gridlayout__item__image__caption__title')
    venue_node = article.select_one('.gridlayout__item__image__caption__subtitle')

    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    url = clean_text(title_node.get('href') if title_node else '')
    city = clean_text(city_node.get_text(' ', strip=True) if city_node else '')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    country_name = clean_text(article.get('data-location'))
    country_code = COUNTRY_CODES.get(country_name)
    dates = parse_dates(date_node.get_text(' ', strip=True) if date_node else '')
    description_node = article.select_one('.grlt_smalldesc')
    description = clean_text(description_node) or None

    if not all((title, url, city, venue, country_code, dates)):
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
            'description': description,
        }
        for event_date in dates
    ]


class JuanDiegoFlorezComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='juandiegoflorez_com',
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
        response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for article in soup.select('article.gridlayout__item'):
            parsed = parse_event(article)
            if parsed:
                records.extend(parsed)
            else:
                title_node = article.select_one('.grlt_title a')
                log_message(
                    'Skipped incomplete Juan Diego Flórez schedule entry',
                    event='crawler_item_skipped',
                    level='warning',
                    url=title_node.get('href', '') if title_node else SCHEDULE_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    JuanDiegoFlorezComCrawler().run()


if __name__ == '__main__':
    main()
