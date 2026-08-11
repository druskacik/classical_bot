import re
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ensemblepygmalion.com/'
SOURCE = 'Pygmalion'
AGENDA_URL = f'{SOURCE_URL}agenda/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}
COUNTRY_CODES = {
    'allemagne': 'DE',
    'angleterre': 'GB',
    'autriche': 'AT',
    'belgique': 'BE',
    'espagne': 'ES',
    'france': 'FR',
    'luxembourg': 'LU',
    'mexique': 'MX',
    'pays-bas': 'NL',
    'suisse': 'CH',
}
# The country displayed beside an occurrence is occasionally stale. These
# unambiguous cities let us correct it without guessing from an address.
CITY_COUNTRY_CODES = {
    'amsterdam': 'NL',
    'utrecht': 'NL',
}
IN_SCOPE_ROW_CLASSES = {'is-concerts', 'is-opera', 'is-operas'}


def clean_text(value):
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value or '')
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def fold(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).casefold())
        if not unicodedata.combining(character)
    )


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', clean_text(value))
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTHS.get(fold(month_name))
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day)).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})h(?:(\d{2}))?', clean_text(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def parse_venue_and_country(value, city):
    text = clean_text(value)
    match = re.fullmatch(r'(.+?)\s*\(([^()]+)\)\s*', text)
    if not match:
        return None, None
    venue, country_name = (part.strip() for part in match.groups())
    country_code = CITY_COUNTRY_CODES.get(fold(city), COUNTRY_CODES.get(fold(country_name)))
    if not venue or fold(venue) == fold(city):
        return None, country_code
    return venue, country_code


def detail_content(soup):
    heading = soup.select_one('main h1')
    title = re.sub(r'\s+', ' ', heading.get_text('', strip=False)).strip() if heading else ''
    content = soup.select_one('main .entry-content')
    if not content:
        return title, None
    for element in content.select('.block-programme-dates, script, style'):
        element.decompose()
    description = clean_text(content)
    return title, description or None


class EnsemblePygmalionComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemblepygmalion_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(AGENDA_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        records = []
        detail_cache = {}
        for row in soup.select('.row-agenda'):
            if not IN_SCOPE_ROW_CLASSES.intersection(row.get('class', [])):
                continue
            detail_link = row.select_one('a.btn-more[href]')
            detail_url = detail_link.get('href', '').strip() if detail_link else ''
            if not detail_url:
                log_message(
                    'Skipped Pygmalion programme without a detail URL',
                    event='crawler_item_skipped', level='warning', url=AGENDA_URL,
                    error_type='IncompleteEventData',
                    error_message='Programme detail URL is missing',
                )
                continue

            if detail_url not in detail_cache:
                detail_response = session.get(detail_url, timeout=45)
                detail_response.raise_for_status()
                detail_cache[detail_url] = detail_content(
                    BeautifulSoup(detail_response.text, 'html.parser')
                )
            title, description = detail_cache[detail_url]

            for occurrence in row.select('.element-programm-date'):
                city = clean_text(occurrence.select_one('.ville'))
                date = parse_date(occurrence.select_one('.date'))
                time_from = parse_time(occurrence.select_one('.time'))
                venue, country_code = parse_venue_and_country(
                    occurrence.select_one('.meta'), city
                )
                if not all((title, date, detail_url, venue, city, country_code)):
                    log_message(
                        'Skipped incomplete Pygmalion occurrence',
                        event='crawler_item_skipped', level='warning', url=detail_url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, city, or country is missing',
                    )
                    continue
                records.append({
                    'title': title,
                    'date': date,
                    'url': detail_url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': country_code,
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['city']
        ))


def main():
    EnsemblePygmalionComCrawler().run()


if __name__ == '__main__':
    main()
