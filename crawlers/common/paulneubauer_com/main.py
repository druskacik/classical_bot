import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.paulneubauer.com/'
SCHEDULE_URL = 'https://www.paulneubauer.com/news-schedule'
SOURCE = 'Paul Neubauer'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI',
    'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI',
    'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC',
    'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT',
    'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}

COUNTRY_NAMES = {
    'austria': 'AT', 'belgium': 'BE', 'canada': 'CA', 'china': 'CN',
    'czech republic': 'CZ', 'czechia': 'CZ', 'denmark': 'DK',
    'england': 'GB', 'finland': 'FI', 'france': 'FR', 'germany': 'DE',
    'hungary': 'HU', 'italy': 'IT', 'japan': 'JP', 'netherlands': 'NL',
    'norway': 'NO', 'poland': 'PL', 'portugal': 'PT', 'scotland': 'GB',
    'south korea': 'KR', 'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'taiwan': 'TW', 'united kingdom': 'GB', 'united states': 'US',
}

# The schedule currently abbreviates this German state instead of the country.
REGION_COUNTRIES = {'saxony': 'DE'}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(element):
    if element is None:
        return None
    month_text = ''.join(element.find_all(string=True, recursive=False)).strip().lower()
    day = clean_text(element.select_one('.day'))
    year = clean_text(element.select_one('.year'))
    month = MONTHS.get(month_text)
    if not month or not day.isdigit() or not year.isdigit():
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def parse_location(value):
    match = re.fullmatch(
        r'\s*([^|,]+?)\s*,\s*([^|,]+?)\s*(?:\|\s*([^|]+))?\s*', value or ''
    )
    if not match:
        return None
    city, region, time_text = (part.strip() if part else '' for part in match.groups())
    if not city or not region:
        return None

    region_key = region.lower().rstrip('.')
    if region.upper() in US_REGIONS:
        country_code = 'US'
    else:
        country_code = COUNTRY_NAMES.get(region_key) or REGION_COUNTRIES.get(region_key)
    if not country_code:
        return None

    time_match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', time_text, re.I)
    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).lower() == 'p':
            hour += 12
        time_from = f'{hour:02d}:{int(time_match.group(2) or 0):02d}'
    return city, country_code, time_from


def parse_event(element):
    event_date = parse_date(element.select_one('.date'))
    venue = clean_text(element.select_one('.venue'))
    location = parse_location(clean_text(element.select_one('.city')))
    link = element.select_one('.link a[href]')
    if not event_date or not venue or not location or link is None:
        return None

    description_parts = []
    summary = clean_text(element.select_one('.desc'))
    if summary:
        description_parts.append(summary)
    players = element.select_one('.players')
    if players is not None:
        player_lines = []
        for name in players.select('dt'):
            role = name.find_next_sibling('dd')
            line = ' — '.join(part for part in (clean_text(name), clean_text(role)) if part)
            if line:
                player_lines.append(line)
        if player_lines:
            description_parts.append('Performers\n' + '\n'.join(player_lines))

    city, country_code, time_from = location
    title = summary or f'Paul Neubauer at {venue}'
    return {
        'title': title,
        'date': event_date,
        'url': link['href'].strip(),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
    }


class PaulNeubauerComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='paulneubauer_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        try:
            response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Paul Neubauer schedule',
                event='crawler_fetch_failed',
                level='error',
                url=SCHEDULE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = [record for item in soup.select('.events') if (record := parse_event(item))]
        records.sort(key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue'], item['url']
        ))
        if not records:
            log_message(
                'No valid Paul Neubauer events found',
                event='crawler_empty_listing',
                level='warning',
                url=SCHEDULE_URL,
                record_count=0,
            )
        return records


def main():
    PaulNeubauerComCrawler().run()


if __name__ == '__main__':
    main()
