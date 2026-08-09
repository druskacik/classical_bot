import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cameratabern.ch/'
CONCERTS_URL = urljoin(SOURCE_URL, 'konzerte')
PAGINATION_URL = urljoin(SOURCE_URL, 'ajax/pagination_concerts/p{}')
SOURCE = 'CAMERATA BERN'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.6',
}

MONTHS = {
    'jan': 1, 'januar': 1, 'feb': 2, 'februar': 2, 'mär': 3, 'märz': 3,
    'mar': 3, 'april': 4, 'mai': 5, 'juni': 6, 'juli': 7, 'aug': 8,
    'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'okt': 10,
    'oktober': 10, 'nov': 11, 'november': 11, 'dez': 12, 'dezember': 12,
}

# The orchestra is Swiss, but its catalogue also includes tours. Cities not in
# this table are treated as Swiss only when the page supplies a usable venue.
FOREIGN_CITY_COUNTRIES = {
    'amsterdam': 'NL', 'antwerpen': 'BE', 'bad kissingen': 'DE',
    'blaibach': 'DE', 'bonn': 'DE', 'brügge': 'BE', 'budapest': 'HU',
    'bukarest': 'RO', 'celle': 'DE', 'cremona': 'IT', 'edinburgh': 'GB',
    'espinho': 'PT', 'essen': 'DE', 'frankfurt': 'DE', 'genova': 'IT',
    'gent': 'BE', 'grenoble': 'FR', 'hamburg': 'DE', 'hannover': 'DE',
    'heidelberg': 'DE', 'hindsgavl': 'DK', 'hitzacker': 'DE', 'kyoto': 'JP',
    'köln': 'DE', 'london': 'GB', 'monfalcone': 'IT', 'neumarkt': 'DE',
    'novara': 'IT', 'perugia': 'IT', 'prag': 'CZ', 'rotterdam': 'NL',
    'sainokuni': 'JP', 'samobor': 'HR', 'schwetzingen': 'DE',
    'st. peter im schwarzwald': 'DE', 'staufen i. br.': 'DE',
    'timișoara': 'RO', 'tokyo': 'JP', 'vaduz': 'LI',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})\b', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).casefold().rstrip('.'))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_location(value):
    location = clean_text(value)
    if not location:
        return None
    if ',' in location:
        city, venue = (part.strip() for part in location.split(',', 1))
    elif location.casefold().startswith('kirche '):
        venue, city = 'Kirche', location[7:].strip()
    elif location.casefold().startswith('hof schlafhus '):
        city, venue = location.rsplit(' ', 1)[-1], 'Hof Schlafhus'
    else:
        return None
    if not city or not venue or city.casefold() == venue.casefold():
        return None
    return city, venue, FOREIGN_CITY_COUNTRIES.get(city.casefold(), 'CH')


def extract_description(soup):
    sections = []
    for heading in soup.select('h2'):
        if clean_text(heading).casefold() not in {'programm', 'program'}:
            continue
        container = heading.parent
        text = clean_text(container)
        if text:
            sections.append(text)
    return '\n\n'.join(dict.fromkeys(sections)) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1')
    if heading is None:
        return None
    heading = BeautifulSoup(str(heading), 'html.parser')
    for subtype in heading.select('b'):
        subtype.decompose()
    title = clean_text(heading)
    event_date = parse_date(clean_text(soup.select_one('.date')))
    location = parse_location(soup.select_one('.ort'))
    if not title or not event_date or not location:
        return None
    city, venue, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(soup.select_one('.uhrzeit'))),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': extract_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class CameratabernChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cameratabern_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = []
        seen_urls = set()
        seen_page_signatures = set()

        for page in range(1, 201):
            url = CONCERTS_URL if page == 1 else PAGINATION_URL.format(page)
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            page_urls = list(dict.fromkeys(
                urljoin(SOURCE_URL, link['href'])
                for link in soup.select('a[href*="/konzerte/"][href]')
                if '/ajax/' not in link['href']
            ))
            # The main page preloads the first archive batch, so page 2 can
            # legitimately add nothing. At the end, the endpoint repeats its
            # final page instead of returning an empty response.
            if page > 1:
                signature = tuple(page_urls)
                if not signature or signature in seen_page_signatures:
                    break
                seen_page_signatures.add(signature)
            new_urls = [item for item in page_urls if item not in seen_urls]
            for item in new_urls:
                seen_urls.add(item)
                urls.append(item)

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to process CAMERATA BERN concert',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        log_message(
            'CAMERATA BERN catalogue scraped',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['url']))


def main():
    CameratabernChCrawler().run()


if __name__ == '__main__':
    main()
