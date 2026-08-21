import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.karlheinzstockhausen.org/'
PERFORMANCES_URL = f'{SOURCE_URL}Auffuhrungen_Performances_german.htm'
SOURCE = 'Karlheinz Stockhausen Performances'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
}

# The first-party list is international but does not print countries alongside
# cities. Keep its observed locations explicit rather than guessing geography.
CITY_COUNTRIES = {
    'Berlin': 'DE',
    'Dijon': 'FR',
    'Hamburg': 'DE',
    'Hong Kong': 'HK',
    'Kürten': 'DE',
    'London': 'GB',
    'Munich': 'DE',
    'San Juan': 'AR',
    'São Paulo': 'BR',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.m\.', re.IGNORECASE)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip(' ,')


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == 'p' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def page_year(soup):
    match = re.search(
        r'(?:Aufführungen|Performances)\s+(20\d{2})',
        soup.get_text(' ', strip=True),
        re.IGNORECASE,
    )
    if not match:
        raise ValueError('Could not determine performance-list year')
    return int(match.group(1))


def paragraph_record(paragraph, year):
    date_element = paragraph.find('strong', string=lambda value: value and DATE_RE.search(value))
    if not date_element:
        return None
    date_match = DATE_RE.search(date_element.get_text(' ', strip=True))
    try:
        event_date = date(
            year,
            datetime.strptime(date_match.group(1), '%B').month,
            int(date_match.group(2)),
        ).isoformat()
    except ValueError:
        return None

    city_element = date_element.find_next('strong')
    city = clean_text(city_element.get_text(' ', strip=True)) if city_element else ''
    venue_element = city_element.find_next('em') if city_element else None
    venue = clean_text(venue_element.get_text(' ', strip=True)) if venue_element else ''
    country_code = CITY_COUNTRIES.get(city)
    if not city or not venue or not country_code:
        log_message(
            'Skipping performance with unresolved location',
            event='crawler_item_skipped',
            level='warning',
            city=city or None,
            venue=venue or None,
            url=PERFORMANCES_URL,
        )
        return None

    titles = []
    for element in venue_element.find_all_next('strong'):
        if element.find_parent('p') is not paragraph:
            break
        title = clean_text(element.get_text(' ', strip=True))
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return None

    link = paragraph.find('a', href=re.compile(r'^https?://'))
    url = link.get('href').strip() if link else PERFORMANCES_URL
    description = clean_text(paragraph.get_text(' ', strip=True)) or None
    time_from = parse_time(paragraph.get_text(' ', strip=True))

    return {
        'title': ' / '.join(titles),
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


def get_concerts():
    response = requests.get(PERFORMANCES_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    year = page_year(soup)
    records = [paragraph_record(paragraph, year) for paragraph in soup.find_all('p')]
    records = [record for record in records if record]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class KarlheinzStockhausenOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='karlheinzstockhausen_org',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KarlheinzStockhausenOrgCrawler().run()


if __name__ == '__main__':
    main()
