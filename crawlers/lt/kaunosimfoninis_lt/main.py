import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kaunosimfoninis.lt/'
SOURCE = 'Kauno miesto simfoninis orkestras'
SITEMAP_URL = 'https://kaunosimfoninis.lt/repertuaras-sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'lt-LT,lt;q=0.9,en;q=0.5',
}

# The site normally prints only the hall, not its city. These are places used
# in the orchestra's repertoire archive; matching the displayed place is safer
# than assigning the orchestra's home city to touring performances.
PLACE_CITIES = {
    'Kauno valstybin': ('Kaunas', 'LT'),
    'Kauno filharmon': ('Kaunas', 'LT'),
    'Kauno miesto': ('Kaunas', 'LT'),
    'Kauno kultūros centr': ('Kaunas', 'LT'),
    'Kauno sporto hal': ('Kaunas', 'LT'),
    'Kauno Žalgirio aren': ('Kaunas', 'LT'),
    'Žalgirio aren': ('Kaunas', 'LT'),
    'VDU Didžioji aul': ('Kaunas', 'LT'),
    'Vytauto Didžiojo universiteto Didži': ('Kaunas', 'LT'),
    'Pažaislio vienuolyn': ('Kaunas', 'LT'),
    'M. Žilinsko dailės galer': ('Kaunas', 'LT'),
    'Girstučio': ('Kaunas', 'LT'),
    'Prisikėlimo bazilik': ('Kaunas', 'LT'),
    'Šv. arkangelo Mykolo': ('Kaunas', 'LT'),
    'Šv. Jurgio Kankinio': ('Kaunas', 'LT'),
    'Vilniaus': ('Vilnius', 'LT'),
    'Valdovų rūm': ('Vilnius', 'LT'),
    'Kongresų rūm': ('Vilnius', 'LT'),
    'Compensa': ('Vilnius', 'LT'),
    'Palangos': ('Palanga', 'LT'),
    'Klaipėdos': ('Klaipėda', 'LT'),
    'Šiaulių': ('Šiauliai', 'LT'),
    'Panevėžio': ('Panevėžys', 'LT'),
    'Marijampolės': ('Marijampolė', 'LT'),
    'Birštono': ('Birštonas', 'LT'),
    'Tytuvėnų': ('Tytuvėnai', 'LT'),
    'Šiluvos': ('Šiluva', 'LT'),
    'Raudondvario': ('Raudondvaris', 'LT'),
    'Zapyškio': ('Zapyškis', 'LT'),
    'Jonavos': ('Jonava', 'LT'),
    'Kėdainių': ('Kėdainiai', 'LT'),
    'Alytaus': ('Alytus', 'LT'),
}

TOUR_CITIES = {
    'Viljandi': ('Viljandi', 'EE'),
    'Torun': ('Toruń', 'PL'),
    'Toruń': ('Toruń', 'PL'),
    'Rygos': ('Ryga', 'LV'),
    'Riga': ('Ryga', 'LV'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit(('https', 'kaunosimfoninis.lt', parts.path, parts.query, ''))


def get_event_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = []
    for node in soup.select('loc'):
        url = canonical_url(node.get_text(strip=True))
        path = urlsplit(url).path.rstrip('/')
        if '/repertuaras/' not in path or path.startswith('/en/'):
            continue
        if path == '/repertuaras':
            continue
        urls.append(url)
    return list(dict.fromkeys(urls))


def event_datetime(soup):
    schema = soup.select_one('script.yoast-schema-graph')
    if schema and schema.string:
        try:
            payload = json.loads(schema.string)
            for item in payload.get('@graph', []):
                value = item.get('datePublished')
                if value:
                    return datetime.fromisoformat(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return None


def resolve_city(venue, evidence):
    haystack = f'{venue}\n{evidence}'
    for marker, result in TOUR_CITIES.items():
        if marker.casefold() in haystack.casefold():
            return result
    for marker, result in PLACE_CITIES.items():
        if marker.casefold() in venue.casefold():
            return result
    return None, None


def make_record(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.concert-title'))
    venue = clean_text(soup.select_one('.concert-place'))
    starts_at = event_datetime(soup)
    if starts_at and starts_at.tzinfo:
        starts_at = starts_at.astimezone(ZoneInfo('Europe/Vilnius'))

    content_parts = []
    for block in soup.select('.concert-main-inner .concert-block1, .concert-main-inner .concert-block2'):
        text = clean_text(block)
        if text and text not in content_parts:
            content_parts.append(text)
    description = '\n\n'.join(content_parts) or None
    city, country_code = resolve_city(venue, f'{title}\n{description or ""}')

    if not title or not venue or not starts_at or not city or not country_code:
        return None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return make_record(url, response.text)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = get_event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_record, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_detail_failed',
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
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class KaunoSimfoninisLtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kaunosimfoninis_lt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KaunoSimfoninisLtCrawler().run()


if __name__ == '__main__':
    main()
