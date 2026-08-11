import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lebalcon.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'fr/calendrier')
SOURCE = 'Le Balcon'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janv': 1, 'janvier': 1, 'fevr': 2, 'fevrier': 2, 'mars': 3,
    'avr': 4, 'avril': 4, 'mai': 5, 'juin': 6, 'juil': 7,
    'juillet': 7, 'aout': 8, 'sept': 9, 'septembre': 9, 'oct': 10,
    'octobre': 10, 'nov': 11, 'novembre': 11, 'dec': 12,
    'decembre': 12,
}

# Le Balcon is a French ensemble, but its own calendar also contains tours.
# These are foreign cities actually present in the published archive.
FOREIGN_CITY_COUNTRIES = {
    'barranquilla': 'CO', 'berlin': 'DE', 'bogota': 'CO',
    'bruxelles': 'BE', 'buenos aires': 'AR', 'cartagena de indias': 'CO',
    'hamburg': 'DE', 'kyiv': 'UA', 'kürten': 'DE', 'kurten': 'DE',
    'la chaux-de-fonds': 'CH', 'lviv': 'UA', 'medellin': 'CO',
    'miami': 'US', 'molenbeek-saint-jean': 'BE', 'rome': 'IT',
    'salzburg': 'AT', 'stuttgart': 'DE', 'valencia': 'ES', 'vienne': 'AT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def make_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def fetch_detail(url):
    response = make_session().get(url, timeout=45)
    response.raise_for_status()
    return response.text


def parse_date(value):
    text = normalized(clean_text(value)).replace('.', '')
    match = re.fullmatch(r'(\d{1,2})\s+([a-z]+)\s+(\d{2}|20\d{2})', text)
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    year = int(match.group(3))
    if year < 100:
        year += 2000
    if not month:
        return None
    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(element, fallback=None):
    lines = list(element.stripped_strings) if element else []
    if len(lines) < 2 and fallback:
        lines = list(fallback)
    lines = [clean_text(line) for line in lines if clean_text(line)]
    if len(lines) < 2:
        return None

    venue = lines[0]
    city = re.sub(r'^\d{4,5}\s+', '', lines[-1]).strip(' ,')
    if not venue or not city or venue.casefold() == city.casefold():
        return None

    city_key = normalized(city.split(',', 1)[0].strip())
    country_code = FOREIGN_CITY_COUNTRIES.get(city_key, 'FR')
    return venue, city, country_code


def parse_detail(html, url, fallback_location=None):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.page__name'))
    subtitle = clean_text(soup.select_one('.page__subtitle'))
    location = parse_location(soup.select_one('.page__venue'), fallback_location)
    description = clean_text(soup.select_one('.page__text .wysiwyg'))
    description = '\n\n'.join(part for part in (subtitle, description) if part) or None
    if not title or not location:
        return []

    venue, city, country_code = location
    records = []
    for representation in soup.select('.representations__item'):
        event_date = parse_date(representation.select_one('.representations__period'))
        time_from = clean_text(representation.select_one('.representations__time')) or None
        if time_from and not re.fullmatch(r'\d{1,2}:\d{2}', time_from):
            time_from = None
        if time_from and len(time_from) == 4:
            time_from = '0' + time_from
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class LeBalconComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lebalcon_com',
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
        session = make_session()
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        season_urls = {CALENDAR_URL}
        season_urls.update(
            urljoin(SOURCE_URL, link.get('href'))
            for link in soup.select('a.seasons__anchor[href]')
        )

        productions = {}
        for season_url in sorted(season_urls):
            try:
                season_response = session.get(season_url, timeout=45)
                season_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Le Balcon season',
                    event='crawler_page_failed',
                    level='warning',
                    url=season_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            page = BeautifulSoup(season_response.text, 'html.parser')
            for card in page.select('.productions__item'):
                link = card.select_one('a.productions__anchor[href]')
                place = card.select_one('.productions__place')
                if not link:
                    continue
                url = urljoin(SOURCE_URL, link.get('href'))
                productions[url] = list(place.stripped_strings) if place else None

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_detail, url): (url, fallback)
                for url, fallback in productions.items()
            }
            for future in as_completed(futures):
                url, fallback = futures[future]
                try:
                    parsed = parse_detail(future.result(), url, fallback)
                    if not parsed:
                        log_message(
                            'Skipped incomplete Le Balcon production',
                            event='crawler_item_skipped',
                            level='warning',
                            url=url,
                            error_type='IncompleteEventData',
                            error_message='No complete dated representation or location found',
                        )
                    records.extend(parsed)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Le Balcon production',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    LeBalconComCrawler().run()


if __name__ == '__main__':
    main()
