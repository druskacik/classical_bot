import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://christian-tetzlaff.de/'
SOURCE = 'Christian Tetzlaff'
CALENDAR_URL = f'{SOURCE_URL}konzerte-termine/'
CALENDAR_API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages/125'

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

# The calendar writes English country names even on its German-language page.
COUNTRY_CODES = {
    'Argentina': 'AR', 'Australia': 'AU', 'Austria': 'AT', 'Belgium': 'BE',
    'Brazil': 'BR', 'Bulgaria': 'BG', 'Canada': 'CA', 'Chile': 'CL',
    'China': 'CN', 'Croatia': 'HR', 'Czech Republic': 'CZ', 'Czechia': 'CZ',
    'Denmark': 'DK', 'Estonia': 'EE', 'Finland': 'FI', 'France': 'FR',
    'Germany': 'DE', 'Greece': 'GR', 'Hungary': 'HU', 'Iceland': 'IS',
    'Ireland': 'IE', 'Israel': 'IL', 'Italy': 'IT', 'Japan': 'JP',
    'Latvia': 'LV', 'Lithuania': 'LT', 'Luxembourg': 'LU', 'Mexico': 'MX',
    'Monaco': 'MC', 'Netherlands': 'NL', 'New Zealand': 'NZ', 'Norway': 'NO',
    'Poland': 'PL', 'Portugal': 'PT', 'Romania': 'RO', 'Singapore': 'SG',
    'Slovakia': 'SK', 'Slovenia': 'SI', 'South Korea': 'KR', 'Spain': 'ES',
    'Sweden': 'SE', 'Switzerland': 'CH', 'Taiwan': 'TW', 'Turkey': 'TR',
    'United Kingdom': 'GB', 'United States': 'US',
    'United States of America': 'US',
}

PERFORMER_VENUE_PATTERN = re.compile(
    r'\b(?:orchestra|orchester|orkester|orkest|symphony|symphonieorchester|'
    r'symfoniorkester|philharmonic|philharmonisch(?:e[rs]?)?)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\r', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(event):
    calendar = event.select_one('.qem-calendar-small')
    day = clean_text(calendar.select_one('.day').get_text(' ', strip=True)) if calendar else ''
    nonday = calendar.select_one('.nonday') if calendar else None
    month = clean_text(nonday.select_one('.month').get_text(' ', strip=True)) if nonday else ''
    year_match = re.search(r'\b(20\d{2})\b', nonday.get_text(' ', strip=True)) if nonday else None
    day_match = re.search(r'\b(\d{1,2})\b', day)
    if not all((day_match, month, year_match)):
        return ''
    try:
        return datetime.strptime(
            f'{day_match.group(1)} {month} {year_match.group(1)}', '%d %b %Y'
        ).date().isoformat()
    except ValueError:
        return ''


def parse_location(event):
    location = event.select_one('p.location')
    if not location:
        return '', '', ''
    parts = [clean_text(part) for part in location.stripped_strings if clean_text(part)]
    if len(parts) < 2:
        return '', '', ''
    venue = parts[0]
    city_country = parts[-1]
    if ',' not in city_country:
        return '', '', ''
    city, country = (clean_text(part) for part in city_country.rsplit(',', 1))
    if PERFORMER_VENUE_PATTERN.search(venue):
        return '', '', ''
    return venue, city, COUNTRY_CODES.get(country, '')


def parse_event(event):
    link = event.select_one('.qem-small h2 a[href]')
    title = clean_text(link.get_text(' ', strip=True)) if link else ''
    url = clean_text(link.get('href')) if link else ''
    date = parse_date(event)
    venue, city, country_code = parse_location(event)

    content = event.select_one('.qem-small')
    description_parts = []
    if content:
        for paragraph in content.find_all('p', recursive=False):
            if 'location' in (paragraph.get('class') or []):
                continue
            text = clean_text(paragraph.get_text('\n', strip=True))
            if text and text not in description_parts:
                description_parts.append(text)

    if not all((title, date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': CALENDAR_URL,
        'source': SOURCE,
    }


class ChristianTetzlaffDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='christian_tetzlaff_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CALENDAR_API_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        html = ((response.json().get('content') or {}).get('rendered') or '')
        soup = BeautifulSoup(html, 'html.parser')
        records = []
        for event in soup.select('.qem'):
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                link = event.select_one('h2 a[href]')
                log_message(
                    'Skipped incomplete Christian Tetzlaff event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(link.get('href')) if link else CALENDAR_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


def main():
    ChristianTetzlaffDeCrawler().run()


if __name__ == '__main__':
    main()
