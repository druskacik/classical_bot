import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicaoltenia.ro/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerte')
SOURCE = 'Filarmonica Oltenia Craiova'

MONTHS = {
    'ian': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mai': 5, 'iun': 6,
    'iul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'noi': 11,
    'nov': 11, 'dec': 12,
}

# The calendar normally omits the city because these are well-known Craiova
# venues. Explicit touring locations are resolved separately and are never
# assigned the institution's home city.
CRAIOVA_VENUE_MARKERS = (
    'filarmonic', 'craiova', 'bibliotecii jude', 'muzeul de art',
    'parcul romanescu', 'parcul nicolae romanescu', 'teatrul na',
    'universitatea din craiova',
    'facult', 'biserica madona', 'catedrala sf', 'hotel ramada',
    'centrul multifunc', 'piața mihai viteazul', 'piata mihai viteazul',
    'băncii naţionale', 'bancii nationale', 'fraţii buzeşti',
    'fratii buzesti', 'târgul de crăciun', 'targul de craciun',
    'tipografia', 'sala filip lazăr', 'sala filip lazar', 'silent garden',
)

EXPLICIT_CITY_MARKERS = {
    'românești': 'Românești',
    'romanesti': 'Românești',
    'târgu jiu': 'Târgu Jiu',
    'targu jiu': 'Târgu Jiu',
    'slatina': 'Slatina',
    'calafat': 'Calafat',
    'caracal': 'Caracal',
    'bucurești': 'București',
    'bucuresti': 'București',
    'râmnicu vâlcea': 'Râmnicu Vâlcea',
    'ramnicu valcea': 'Râmnicu Vâlcea',
    'ploiești': 'Ploiești',
    'ploiesti': 'Ploiești',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_html(url):
    response = requests.get(
        url,
        impersonate='chrome',
        headers={'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.7'},
        timeout=45,
    )
    response.raise_for_status()
    return response.text


def parse_datetime(value):
    match = re.search(
        r'\b(\d{1,2})\s+([A-ZĂÂÎȘŞȚŢ]+)\s+(20\d{2})\s*-\s*(\d{1,2}):(\d{2})',
        value.upper(),
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    return event_date, f'{int(match.group(4)):02d}:{match.group(5)}'


def resolve_location(venue):
    folded = venue.casefold()
    if 'bulgaria concert hall' in folded:
        return 'Sofia', 'BG'
    for marker, city in EXPLICIT_CITY_MARKERS.items():
        if marker in folded:
            return city, 'RO'
    if any(marker in folded for marker in CRAIOVA_VENUE_MARKERS):
        return 'Craiova', 'RO'
    # A comma-separated city is explicit enough to use without guessing.
    match = re.search(r',\s*([A-ZĂÂÎȘŞȚŢ][\wĂÂÎȘŞȚŢăâîșşțţ -]{2,})$', venue)
    return (clean_text(match.group(1)), 'RO') if match else (None, None)


def archive_season_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for item in soup.select('.single-property'):
        for anchor in item.select('a[href]'):
            url = urljoin(SOURCE_URL, anchor['href'])
            path = urlparse(url).path.rstrip('/')
            if re.fullmatch(r'/concerte/20\d{2}-20\d{2}', path):
                urls.add(url)
                break
    return sorted(urls)


def season_event_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for item in soup.select('.single-property'):
        anchor = item.select_one('a[href]')
        if not anchor:
            continue
        url = urljoin(SOURCE_URL, anchor['href'])
        path = urlparse(url).path.rstrip('/')
        if len(path.split('/')) >= 4 and path.startswith('/concerte/'):
            urls.add(url)
    return sorted(urls)


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    head = soup.select_one('.property-details-head')
    title = clean_text(head.select_one('h3')) if head else ''
    info = head.select_one('.details-head-com') if head else None
    paragraphs = info.select('p') if info else []
    event_date, time_from = parse_datetime(clean_text(paragraphs[0])) if paragraphs else (None, None)
    venue = clean_text(paragraphs[-1]) if len(paragraphs) >= 2 else ''
    city, country_code = resolve_location(venue) if venue else (None, None)

    description_parts = []
    for panel in soup.select('.col-lg-8 .tab-panel-body'):
        text = clean_text(panel)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    if not title or not event_date or not venue or not city:
        return None
    return {
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
    }


def fetch_event(url):
    return parse_event(get_html(url), url)


class FilarmonicaolteniaRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicaoltenia_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        season_urls = archive_season_urls(get_html(CONCERTS_URL))
        event_urls = set()
        for season_url in season_urls:
            try:
                event_urls.update(season_event_urls(get_html(season_url)))
            except requests.RequestsError as error:
                log_message(
                    'Failed to scrape Filarmonica Oltenia season',
                    event='crawler_page_failed', level='warning', url=season_url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_event, url): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestsError as error:
                    log_message(
                        'Failed to scrape Filarmonica Oltenia concert detail',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Filarmonica Oltenia concert',
                        event='crawler_item_skipped', level='warning', url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, or city is missing',
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    FilarmonicaolteniaRoCrawler().run()


if __name__ == '__main__':
    main()
