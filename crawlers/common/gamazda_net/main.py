import json
import re
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gamazda.net/'
CALENDAR_URL = 'https://gamazda.net/concerts/'
SOURCE = 'Gamazda'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'Austria': 'AT',
    'Czechia': 'CZ',
    'Germany': 'DE',
    'Netherlands': 'NL',
    'North Macedonia': 'MK',
    'Spain': 'ES',
    'UK': 'GB',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def comparable_text(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).casefold())
        if not unicodedata.combining(character)
    )


def parse_calendar_row(section):
    lines = [line for line in clean_text(section).splitlines() if line]
    if len(lines) < 3:
        return None

    try:
        event_date = datetime.strptime(lines[0], '%B %d, %Y').date().isoformat()
    except ValueError:
        return None

    location = ' '.join(lines[1:-1])
    if ',' not in location:
        return None
    city, country = (part.strip() for part in location.rsplit(',', 1))
    country_code = COUNTRY_CODES.get(country)
    link = section.select_one('a[href]')
    if not city or not country_code or link is None:
        return None

    return {
        'date': event_date,
        'city': city,
        'country_code': country_code,
        'detail_url': link['href'],
    }


def iter_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                yield item


def parse_dice_detail(soup):
    for item in iter_json_ld(soup):
        if item.get('@type') not in ('Event', 'MusicEvent'):
            continue
        location = item.get('location') or {}
        start = item.get('startDate') or ''
        if not location.get('name') or not start:
            continue
        try:
            start_at = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except ValueError:
            continue
        return {
            'title': clean_text(item.get('name')),
            'date': start_at.date().isoformat(),
            'time_from': start_at.strftime('%H:%M'),
            'venue': clean_text(location.get('name')),
            'description': clean_text(item.get('description')) or None,
        }
    return None


def parse_ticketstream_detail(soup):
    text = clean_text(soup)
    heading = soup.select_one('h1')
    title = clean_text(heading)
    title_text = clean_text(soup.title)
    venue_match = re.search(
        r',\s*([^,]+),\s*Hradec (?:Králové|Kralove)\s*-\s*(?:Vstupenky|Tickets)',
        title_text,
    )
    date_match = re.search(r'\b(\d{1,2})\.\s*(\w+)\s+(20\d{2})\b', text)
    if not title or not venue_match or not date_match:
        return None

    months = {
        'ledna': 1, 'února': 2, 'března': 3, 'dubna': 4, 'května': 5,
        'června': 6, 'července': 7, 'srpna': 8, 'září': 9, 'října': 10,
        'listopadu': 11, 'prosince': 12,
    }
    month = months.get(date_match.group(2).lower())
    if month is None:
        return None
    try:
        event_date = datetime(
            int(date_match.group(3)), month, int(date_match.group(1))
        ).date().isoformat()
    except ValueError:
        return None

    description_match = re.search(
        r'(?:Popis|Description)\n(.+?)(?:\nDoplňující informace|\nAdditional info|\nPořadatel|\nPromoter)',
        text,
        re.S,
    )
    return {
        'title': title,
        'date': event_date,
        'time_from': None,
        'venue': venue_match.group(1).strip(),
        'description': clean_text(description_match.group(1)) if description_match else None,
    }


def parse_detail(soup, url):
    if 'dice.fm/' in url:
        return parse_dice_detail(soup)
    if 'ticketstream.cz/' in url:
        return parse_ticketstream_detail(soup)
    return None


class GamazdaNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gamazda_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Gamazda concert calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for section in soup.select('section.elementor-inner-section'):
            row = parse_calendar_row(section)
            if row is None:
                continue
            try:
                detail_response = session.get(row['detail_url'], timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Gamazda ticket detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=row['detail_url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
            detail = parse_detail(detail_soup, detail_response.url)
            if (
                detail is None
                or detail['date'] != row['date']
                or comparable_text(row['city']) not in comparable_text(detail_soup)
            ):
                log_message(
                    'Skipping Gamazda row without matching venue detail',
                    event='crawler_record_skipped',
                    level='warning',
                    url=row['detail_url'],
                    date=row['date'],
                    city=row['city'],
                )
                continue

            records.append({
                'title': detail['title'],
                'date': row['date'],
                'url': row['detail_url'],
                'time_from': detail['time_from'],
                'venue': detail['venue'],
                'city': row['city'],
                'country_code': row['country_code'],
                'description': detail['description'],
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(records, key=lambda record: (record['date'], record['time_from'] or ''))


def main():
    GamazdaNetCrawler().run()


if __name__ == '__main__':
    main()
