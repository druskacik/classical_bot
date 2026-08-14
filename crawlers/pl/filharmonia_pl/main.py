import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonia.pl/'
SITEMAP_URL = f'{SOURCE_URL}sitemap'
SOURCE = 'Filharmonia Narodowa'
DEFAULT_CITY = 'Warszawa'
DEFAULT_VENUES = {'Sala Kameralna', 'Sala Koncertowa'}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

# The repertoire includes occasional performances by the Warsaw Philharmonic
# outside Poland. Venue strings consistently end in the city; these are the
# Polish or local spellings seen in the site's touring archive.
FOREIGN_CITY_COUNTRIES = {
    'ankara': 'TR',
    'berlin': 'DE',
    'berno': 'CH',
    'bruksela': 'BE',
    'budapeszt': 'HU',
    'hamburg': 'DE',
    'londyn': 'GB',
    'lucerna': 'CH',
    'monachium': 'DE',
    'paryż': 'FR',
    'praga': 'CZ',
    'seul': 'KR',
    'tokio': 'JP',
    'wiedeń': 'AT',
    'wilno': 'LT',
    'zurych': 'CH',
}

CITY_NORMALIZATIONS = {
    'białymstoku': 'Białystok',
    'bydgoszczy': 'Bydgoszcz',
    'gdańsku': 'Gdańsk',
    'katowicach': 'Katowice',
    'krakowie': 'Kraków',
    'lublinie': 'Lublin',
    'lusławicach': 'Lusławice',
    'łodzi': 'Łódź',
    'poznaniu': 'Poznań',
    'szczecinie': 'Szczecin',
    'toruniu': 'Toruń',
    'warszawie': 'Warszawa',
    'wrocławiu': 'Wrocław',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_date_from_page(soup):
    back_link = soup.select_one('a.event-link-back[href*="ts:"]')
    if not back_link:
        return None
    match = re.search(r'ts:(\d+)', back_link.get('href', ''))
    if not match:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def location_from_page(soup, title):
    venue = clean_text(soup.select_one('.event-venue-price .venue-str'))
    if not venue:
        return None

    categories = clean_text(soup.select_one('.event-categories')).lower()
    is_touring = 'poza siedzibą' in categories
    if venue in DEFAULT_VENUES or not is_touring:
        city = venue.rsplit(',', 1)[-1].strip() if ',' in venue else DEFAULT_CITY
    elif ',' in venue:
        city = venue.rsplit(',', 1)[-1].strip()
    else:
        # Touring venues and titles commonly end with "w <city>".
        match = re.search(
            r'\bw\s+([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż -]+)$',
            venue,
        )
        if not match:
            match = re.search(
                r'\bw\s+([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż -]+)$',
                title,
            )
        city = match.group(1).strip() if match else ''
    if not city:
        return None
    city = CITY_NORMALIZATIONS.get(city.casefold(), city)
    country_code = FOREIGN_CITY_COUNTRIES.get(city.casefold(), 'PL')
    return venue, city, country_code


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main .title-in-sidebar'))
    event_date = event_date_from_page(soup)
    location = location_from_page(soup, title)
    if not title or not event_date or not location:
        return None

    time_text = clean_text(soup.select_one('.event-meta-date-full .time'))
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)', time_text)
    time_from = f'{int(match.group(1)):02d}:{match.group(2)}' if match else None

    description_parts = []
    for selector in (
        '.event-meta-categorie-links',
        '.event-meta-performers',
        '.event-meta-composer',
        '.event-content .content-attr',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, response.url.split(',lp:', 1)[0])


class FilharmoniaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.text, 'xml')
        urls = sorted({
            clean_text(loc)
            for loc in sitemap.select('loc')
            if '/repertuar/' in clean_text(loc)
        })

        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Filharmonia Narodowa event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    FilharmoniaPlCrawler().run()


if __name__ == '__main__':
    main()
