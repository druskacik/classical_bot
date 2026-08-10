import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.g-h-t.de/'
SOURCE = 'Gerhart-Hauptmann-Theater Görlitz-Zittau'
CALENDAR_URL = urljoin(SOURCE_URL, 'de/spielplan/')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}

VENUE_CITIES = {
    'haus görlitz': 'Görlitz',
    'apollo görlitz': 'Görlitz',
    'alter schlachthof görlitz': 'Görlitz',
    'haus zittau': 'Zittau',
    'marktplatz zittau': 'Zittau',
    'theater zittau': 'Zittau',
    'volkstheater bautzen': 'Bautzen',
    'lausitzhalle hoyerswerda': 'Hoyerswerda',
    'kreuzkirche weißwasser': 'Weißwasser',
    'lichtsaal telux weißwasser': 'Weißwasser',
    'schloss krobnitz': 'Reichenbach/O.L.',
    'bürgerhaus niesky': 'Niesky',
    'stadttheater kamenz': 'Kamenz',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    path = parts.path.rstrip('/') + '/'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def resolve_city(venue):
    folded = venue.casefold()
    for venue_name, city in VENUE_CITIES.items():
        if venue_name in folded:
            return city
    return None


def clean_venue(venue):
    # One listing appends the street address to the otherwise unambiguous venue.
    if venue.casefold().startswith('schloss krobnitz,'):
        return venue.split(',', 1)[0].strip()
    return venue


def parse_listing(entry):
    link = entry.select_one('.info a[href]')
    info = entry.select_one('.info')
    if not link or not info:
        return None

    url = canonical_url(link['href'])
    date_match = re.search(r'/(\d{4}-\d{2}-\d{2})/\d+/$', urlsplit(url).path)
    title = clean_text(link.select_one('span') or link)
    detail_line = clean_text(info.find_all('div', recursive=False)[-1])
    parts = [part.strip() for part in detail_line.split('|')]
    time_match = re.search(r'\b(\d{1,2}:\d{2})\s*Uhr\b', parts[0] if parts else '')
    venue = clean_venue(parts[1]) if len(parts) > 1 else ''
    city = resolve_city(venue)
    if not title or not date_match or not venue or not city:
        return None
    try:
        date.fromisoformat(date_match.group(1))
    except ValueError:
        return None

    subtitle = clean_text(info.select_one('em'))
    return {
        'title': title,
        'date': date_match.group(1),
        'url': url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': subtitle or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(url, fallback):
    soup = get_soup(url)
    root = soup.select_one('.ghtSpielplanDetail')
    if not root:
        return fallback

    parts = []
    for node in root.select(
        ':scope > .templ_table h1 + em, '
        ':scope > .templ_table .templ_td_2pic > .box_txt, '
        '.ght_detail_block .box_txt, '
        '.ght_detail_accordion.besetzung .content'
    ):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or fallback


def discover_records():
    soup = get_soup(CALENDAR_URL)
    months = {
        option.get('value')
        for option in soup.select('#filterMonat option[value]')
        if re.fullmatch(r'\d{4}-\d{2}', option.get('value', ''))
    }
    records_by_url = {}
    for month in sorted(months):
        month_url = urljoin(CALENDAR_URL, f'-/{month}/')
        month_soup = get_soup(month_url)
        for entry in month_soup.select('.ght_spielplan .entry'):
            record = parse_listing(entry)
            if record:
                records_by_url[record['url']] = record
    return records_by_url


def production_key(url):
    parts = urlsplit(url).path.rstrip('/').split('/')
    return '/'.join(parts[:-2])


class GhtDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='g_h_t_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        records_by_url = discover_records()
        production_urls = {}
        for url in records_by_url:
            production_urls.setdefault(production_key(url), url)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(
                    detail_description, url, records_by_url[url]['description']
                ): (key, url)
                for key, url in production_urls.items()
            }
            for future in as_completed(futures):
                key, url = futures[future]
                try:
                    description = future.result()
                    for record_url, record in records_by_url.items():
                        if production_key(record_url) == key:
                            record['description'] = description
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape GHT event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(records_by_url.values(), key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    GhtDeCrawler().run()


if __name__ == '__main__':
    main()
