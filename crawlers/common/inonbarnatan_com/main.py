import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.inonbarnatan.com/'
SCHEDULE_URL = f'{SOURCE_URL}schedule'
SOURCE = 'Inon Barnatan'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
}

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}
US_REGION_NAMES = {
    'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado',
    'connecticut', 'delaware', 'florida', 'georgia', 'hawaii', 'idaho',
    'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana',
    'maine', 'maryland', 'massachusetts', 'michigan', 'minnesota',
    'mississippi', 'missouri', 'montana', 'nebraska', 'nevada',
    'new hampshire', 'new jersey', 'new mexico', 'new york',
    'north carolina', 'north dakota', 'ohio', 'oklahoma', 'oregon',
    'pennsylvania', 'rhode island', 'south carolina', 'south dakota',
    'tennessee', 'texas', 'utah', 'vermont', 'virginia', 'washington',
    'west virginia', 'wisconsin', 'wyoming', 'district of columbia',
}
CA_REGIONS = {'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'}
COUNTRY_NAMES = {
    'austria': 'AT', 'belgium': 'BE', 'canada': 'CA', 'france': 'FR',
    'germany': 'DE', 'israel': 'IL', 'italy': 'IT', 'netherlands': 'NL',
    'spain': 'ES', 'switzerland': 'CH', 'united kingdom': 'GB',
    'united states': 'US', 'usa': 'US',
}


def clean_text(node):
    if not node:
        return ''
    value = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', value.replace('\u200d', ' ')).strip()


def parse_location(value):
    location = clean_text(value)
    if not location or ',' not in location:
        return None, None
    city, region = (part.strip() for part in location.rsplit(',', 1))
    if not city or not region:
        return None, None
    upper_region = region.upper()
    if upper_region in US_REGIONS or region.casefold() in US_REGION_NAMES:
        return city, 'US'
    if upper_region in CA_REGIONS:
        return city, 'CA'
    country_code = COUNTRY_NAMES.get(region.casefold())
    return (city, country_code) if country_code else (None, None)


def parse_date(value):
    text = clean_text(value)
    # A hyphenated date range represents a festival/season overview, not a
    # concrete performance occurrence.
    if re.search(r'\b\d{1,2}\s*[-–—]\s*(?:[A-Za-z]+\s+)?\d{1,2}\b', text):
        return None
    for fmt in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def records_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.schedule-text8_item'):
        title = clean_text(item.select_one('h3'))
        venue = clean_text(item.select_one('.subtitle.text-weight-medium'))
        location = clean_text(item.select_one('.subtitle.small-text'))
        city, country_code = parse_location(location)
        link = item.select_one('a[href]')
        url = link.get('href', '').strip() if link else ''
        description = clean_text(item.select_one('.w-richtext')) or None

        if not all((title, venue, city, country_code, url)):
            continue

        date_nodes = item.select('.schedule-text8_col:nth-of-type(2) .subtitle')
        for date_node in date_nodes:
            date = parse_date(date_node)
            if not date:
                continue
            records.append({
                'title': title,
                'date': date,
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
            })
    return sorted(records, key=lambda record: (record['date'], record['title'], record['venue']))


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(SCHEDULE_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    records = records_from_html(response.text)
    if not records:
        log_message(
            'No concrete Inon Barnatan performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SCHEDULE_URL,
            record_count=0,
        )
    return records


class InonbarnatanComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='inonbarnatan_com',
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
        return scrape_concerts()


def main():
    InonbarnatanComCrawler().run()


if __name__ == '__main__':
    main()
