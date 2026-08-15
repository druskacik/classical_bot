import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.osn.gov.py/'
SOURCE = 'Orquesta Sinfónica Nacional del Paraguay'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-PY,es;q=0.9',
}
MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
    'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
    'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def published_year(soup):
    element = soup.select_one('.fecha-publi')
    match = re.search(r'20\d{2}', clean_text(element))
    return int(match.group()) if match else date.today().year


def parse_event_date(text, reference_year):
    match = re.search(
        r'\b(?:lunes|martes|mi(?:e|\u00e9)rcoles|jueves|viernes|s(?:a|\u00e1)bado|domingo)'
        r'(?:\s+\d{1,2})?\s+(\d{1,2})\s+de\s+'
        r'(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)'
        r'(?:\s+de\s+(20\d{2}))?',
        text,
        re.I,
    )
    if not match:
        return None
    year = int(match.group(3) or reference_year)
    month = MONTHS[match.group(2).lower()]
    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b(?:a\s+las|desde\s+las)\s+(\d{1,2})(?::(\d{2}))?\s*(?:h(?:s)?\.?)?', text, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    return f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None


def parse_location(text):
    def normalized(venue, city):
        venue = re.sub(r'\s+', ' ', venue).strip(' ,')
        if venue.lower() in {'ciudad', 'ciudad de'}:
            return None, city
        if city.lower() in {'asunción', 'asuncion'} and re.search(r'teatro municipal', venue, re.I):
            return 'Teatro Municipal Ignacio A. Pane', 'Asunción'
        return venue, city

    patterns = [
        r'\ben\s+(?:el|la)\s+([^.;\n]+?)(?:,|\s+de\s+la\s+ciudad\s+de|\s+de\s+)(Asunci[o\u00f3]n|San Bernardino|Itaugu[a\u00e1]|Capiat[a\u00e1]|Encarnaci[o\u00f3]n|Caacup[e\u00e9]|Villarrica|Luque|Fernando de la Mora)\b',
        r'\ben\s+(?:el|la)\s+([^.;\n,]+),\s*(?:ubicad[oa]\s+en\s+)?(?:la\s+ciudad\s+de\s+)?(Asunci[o\u00f3]n|San Bernardino|Itaugu[a\u00e1]|Capiat[a\u00e1]|Encarnaci[o\u00f3]n|Caacup[e\u00e9]|Villarrica|Luque|Fernando de la Mora)\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            venue = re.sub(r'\s+', ' ', match.group(1)).strip(' ,')
            city = match.group(2)
            if 2 < len(venue) < 140:
                return normalized(venue, city)

    # The municipal theatre's name often contains the city rather than following it.
    match = re.search(
        r'\ben\s+(?:el|la)\s+(Teatro Municipal(?: de Asunci[o\u00f3]n)?\s*["\u201c]?Ignacio A\. Pane["\u201d]?)',
        text,
        re.I,
    )
    if match:
        return normalized(match.group(1), 'Asunción')

    # Some notices name a city-wide outdoor event but omit a formal venue.
    match = re.search(r'\ben\s+la\s+ciudad\s+de\s+(San Bernardino|Itaugu[a\u00e1]|Capiat[a\u00e1])\b', text, re.I)
    if match:
        return None, match.group(1)
    return None, None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.contenido_contenido .contenido')
    if not content:
        return None
    title = clean_text(content.select_one('.title-not'))
    body = clean_text(content.select_one('.contenido-desc'))
    if not title or not body:
        return None
    event_date = parse_event_date(body, published_year(soup))
    venue, city = parse_location(body)
    if not event_date or not venue or not city:
        return None
    return {
        'title': title.rstrip('.'),
        'date': event_date,
        'url': url,
        'time_from': parse_time(body),
        'venue': venue,
        'city': city,
        'country_code': 'PY',
        'description': body,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for anchor in soup.select('a[href]'):
        href = anchor.get('href', '').strip()
        if href.startswith('/noticias/'):
            url = urljoin(SOURCE_URL, href)
            if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
                links.add(url)
    return links


class OsnGovPyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osn_gov_py',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PY',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        index_urls = [SOURCE_URL]
        # These static year pages are the site's only surviving archive/index.
        index_urls.extend(f'{SOURCE_URL}temporada-{year}' for year in range(date.today().year, 2017, -1))
        links = set()
        for index_url in index_urls:
            try:
                response = requests.get(index_url, headers=HEADERS, timeout=45)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                links.update(detail_links(response.text))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch OSN concert index', event='crawler_index_failed',
                    level='warning', url=index_url, error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(requests.get, url, headers=HEADERS, timeout=45): url
                for url in links
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    record = parse_detail(response.text, url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch OSN news detail', event='crawler_item_failed',
                        level='warning', url=url, error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    OsnGovPyCrawler().run()


if __name__ == '__main__':
    main()
