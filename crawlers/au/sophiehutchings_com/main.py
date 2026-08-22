from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sophiehutchings.com/'
LIVE_URL = f'{SOURCE_URL}live/'
SOURCE = 'Sophie Hutchings'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-AU,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU',
    'belgium': 'BE',
    'belguim': 'BE',  # Misspelling currently used by the source.
    'england': 'GB',
    'germany': 'DE',
    'hungary': 'HU',
    'ireland': 'IE',
    'netherlands': 'NL',
    'portugal': 'PT',
    'united kingdom': 'GB',
    'uk': 'GB',
}

# Some cards omit the country even though the locality identifies it clearly.
CITY_COUNTRIES = {
    'amsterdam': 'NL',
    'berlin': 'DE',
    'budapest': 'HU',
    'dublin': 'IE',
    'hobart': 'AU',
    'leuven': 'BE',
    'london': 'GB',
    'melbourne': 'AU',
    'rotterdam': 'NL',
    'setúbal': 'PT',
    'sydney': 'AU',
}


def clean_text(element):
    if element is None:
        return ''
    return ' '.join(element.get_text(' ', strip=True).split())


def country_code_for(card, city):
    country = clean_text(card.select_one('[itemprop="addressCountry"]')).lower()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]

    locality = city.lower()
    for name, code in COUNTRY_CODES.items():
        if locality.endswith(f', {name}'):
            return code
    return CITY_COUNTRIES.get(locality.split(',')[0].strip())


def parse_card(card):
    date_element = card.select_one('time[datetime]')
    permalink = card.select_one('a.gig-permalink[href]')
    venue = clean_text(card.select_one('[itemprop="location"] [itemprop="name"]'))
    city = clean_text(card.select_one('[itemprop="addressLocality"]'))
    note = clean_text(card.select_one('.gig-note'))

    if not date_element or not permalink or not venue or not city:
        return None

    raw_datetime = date_element.get('datetime', '')
    try:
        parsed_datetime = datetime.fromisoformat(raw_datetime)
    except (TypeError, ValueError):
        return None

    country_code = country_code_for(card, city)
    if not country_code:
        return None

    for country_name in COUNTRY_CODES:
        suffix = f', {country_name}'
        if city.lower().endswith(suffix):
            city = city[:-len(suffix)].strip()
            break

    title = note or clean_text(card.select_one('.gig-name'))
    if not title:
        return None

    return {
        'title': title,
        'date': parsed_datetime.date().isoformat(),
        'url': permalink['href'],
        'time_from': parsed_datetime.strftime('%H:%M') if 'T' in raw_datetime else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': note or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SophieHutchingsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sophiehutchings_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AU',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(LIVE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Sophie Hutchings live events',
                event='crawler_fetch_failed',
                level='error',
                url=LIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for card in soup.select('.gig-card[itemtype="http://schema.org/MusicEvent"]'):
            record = parse_card(card)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SophieHutchingsComCrawler().run()


if __name__ == '__main__':
    main()
