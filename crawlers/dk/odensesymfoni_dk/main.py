import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://odensesymfoni.dk/koncerter/'
SOURCE = 'Odense Symfoniorkester'
SITEMAP_URL = 'https://odensesymfoni.dk/concert-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1,
    'feb': 2,
    'mar': 3,
    'apr': 4,
    'maj': 5,
    'jun': 6,
    'jul': 7,
    'aug': 8,
    'sep': 9,
    'okt': 10,
    'nov': 11,
    'dec': 12,
}

# These venue suffixes are locations within Odense, not city names. Concerts
# with a different comma-separated suffix retain that suffix as their city.
ODENSE_LOCATION_SUFFIXES = {
    'odense',
    'odense koncerthus',
    'odeon',
}


def clean_text(value):
    if not value:
        return ''
    text = (
        html.unescape(str(value))
        .replace('\xa0', ' ')
        .replace('\u200b', '')
        .replace('\xad', '')
    )
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\.\s*([a-zæøå]{3,})\.?\s*(\d{4})', clean_text(value).lower())
    if not match:
        return None
    month = MONTHS.get(match.group(2)[:3])
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def extract_label(container, label):
    strong = next(
        (
            node
            for node in container.select('strong')
            if clean_text(node.get_text(' ', strip=True)).rstrip(':').lower() == label.lower()
        ),
        None,
    )
    if strong is None:
        return ''
    values = []
    for sibling in strong.next_siblings:
        if getattr(sibling, 'name', None) == 'br':
            break
        values.append(sibling.get_text(' ', strip=True) if hasattr(sibling, 'get_text') else str(sibling))
    return clean_text(' '.join(values)).lstrip(':').strip()


def city_from_venue(venue):
    parts = [part.strip() for part in venue.split(',') if part.strip()]
    if len(parts) < 2:
        return 'Odense'
    suffix = re.sub(r'^\d{4}\s+', '', parts[-1]).strip()
    if suffix.lower() in ODENSE_LOCATION_SUFFIXES:
        return 'Odense'
    return suffix


def get_description(soup):
    parts = []
    intro = soup.select_one('.concert-single-intro-text-content')
    if intro:
        intro_soup = BeautifulSoup(str(intro), 'html.parser')
        for unwanted in intro_soup.select('h1, h2, .big.underline, script, style'):
            unwanted.decompose()
        intro_text = clean_text(intro_soup.get_text('\n', strip=True))
        intro_text = re.sub(r'(^|\n)VIS MERE($|\n)', '\n', intro_text, flags=re.I)
        if intro_text:
            parts.append(clean_text(intro_text))
    for block in soup.select('.layout-text-content-col'):
        text = clean_text(block.get_text('\n', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_concert_page(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    title_node = soup.select_one('main h1, .concert-single-intro-text-content h1, title')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    title = re.sub(r'\s+-\s+Odense Symfoniorkester$', '', title, flags=re.I)
    description = get_description(soup)
    records = []

    occurrence_nodes = soup.select('.concert-single-events.show-on-less .concert-single-event')
    if not occurrence_nodes:
        occurrence_nodes = soup.select('.concert-single-intro-events .concert-single-event')

    for occurrence in occurrence_nodes:
        date_node = occurrence.select_one('.concert-single-event-date')
        event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
        time_value = extract_label(occurrence, 'Tid')
        time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', time_value)
        venue = extract_label(occurrence, 'Sted')
        city = city_from_venue(venue) if venue else ''
        if not title or not event_date or not venue or not city:
            log_message(
                'Skipped concert occurrence with incomplete event data',
                event='crawler_item_skipped',
                level='warning',
                url=url,
            )
            continue
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
                'venue': venue,
                'city': city,
                'country_code': 'DK',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concert_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return sorted(
        {
            clean_text(node.get_text())
            for node in soup.select('url > loc')
            if '/koncerter/' in node.get_text() and node.get_text().rstrip('/') != SOURCE_URL.rstrip('/')
        }
    )


def fetch_concert(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_concert_page(response.text, response.url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = get_concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_concert, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch concert detail',
                    event='crawler_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class OdenseSymfoniDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='odensesymfoni_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
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
        return get_concerts()


def main():
    OdenseSymfoniDkCrawler().run()


if __name__ == '__main__':
    main()
