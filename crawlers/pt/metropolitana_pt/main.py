import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.metropolitana.pt/'
SITEMAPS = (
    f'{SOURCE_URL}programacao-sitemap.xml',
    f'{SOURCE_URL}programacao-sitemap2.xml',
)
SOURCE = 'Metropolitana'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}

# The listing often omits the municipality for Lisbon venues. These are stable,
# venue-specific defaults; an explicit municipality after a comma wins.
VENUE_CITIES = {
    'academia das ciências de lisboa': 'Lisboa',
    'aula magna': 'Lisboa',
    'centro cultural de belém': 'Lisboa',
    'el corte inglés': 'Lisboa',
    'escola superior de música de lisboa': 'Lisboa',
    'fundação calouste gulbenkian': 'Lisboa',
    'igreja de são roque': 'Lisboa',
    'instituto de higiene e medicina tropical': 'Lisboa',
    'museu do oriente': 'Lisboa',
    'museu nacional da música': 'Lisboa',
    'reitoria da unl': 'Lisboa',
    'teatro tivoli': 'Lisboa',
    'teatro thalia': 'Lisboa',
    'coliseu porto': 'Porto',
    'metropolitana': 'Lisboa',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def sitemap_urls(session):
    urls = set()
    for sitemap_url in SITEMAPS:
        response = session.get(sitemap_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml')
        for location in soup.select('url > loc'):
            url = canonical_url(clean_text(location))
            if url.startswith(f'{SOURCE_URL}programacao/') and url != f'{SOURCE_URL}programacao/':
                urls.add(url)
    return sorted(urls)


def event_year(soup, month, day):
    # Editors consistently date current event artwork YYYY-MM-DD. Prefer this
    # exact first-party date over publication/modified timestamps.
    for element in soup.select('meta[content], img[src]'):
        value = element.get('content') or element.get('src') or ''
        for year, found_month, found_day in re.findall(r'(20\d{2})[-_](\d{2})[-_](\d{2})', value):
            if int(found_month) == month and int(found_day) == day:
                return int(year)

    category = clean_text(soup.select_one('.event-banner__event-category'))
    season = re.search(r'(?:(20)?(\d{2}))\s*[-/]\s*(?:20)?(\d{2})', category)
    if season:
        start = int((season.group(1) or '20') + season.group(2))
        end = int(str(start)[:2] + season.group(3))
        return start if month >= 8 else end

    published = soup.select_one('meta[property="article:published_time"]')
    published_value = published.get('content', '') if published else ''
    if not published_value:
        schema = soup.select_one('script.yoast-schema-graph')
        match = re.search(r'"datePublished"\s*:\s*"(20\d{2}-\d{2})', schema.string or '') if schema else None
        published_value = match.group(1) if match else ''
    if re.match(r'20\d{2}-\d{2}', published_value):
        published_year, published_month = map(int, published_value[:7].split('-'))
        return published_year + (1 if month < published_month and published_month >= 8 else 0)
    return None


def parse_date_time(soup):
    parts = [clean_text(span).casefold().rstrip('.') for span in soup.select('.event-banner__date span')]
    date_part = next((part for part in parts if re.fullmatch(r'\d{1,2}\s+[a-zç]+', part)), '')
    time_part = next((part for part in parts if re.fullmatch(r'\d{1,2}:\d{2}', part)), None)
    match = re.fullmatch(r'(\d{1,2})\s+([a-zç]+)', date_part)
    if not match or match.group(2) not in MONTHS:
        return None, None
    day = int(match.group(1))
    month = MONTHS[match.group(2)]
    year = event_year(soup, month, day)
    if not year:
        return None, None
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None, None
    return event_date, time_part.zfill(5) if time_part else None


def city_from_venue(venue):
    if ',' in venue:
        candidate = venue.rsplit(',', 1)[1].strip()
        if candidate and not re.search(r'\d', candidate) and len(candidate.split()) <= 5:
            city = candidate.title()
            return re.sub(r'\b(Da|De|Do|Das|Dos)\b', lambda match: match.group(1).lower(), city)
    folded = venue.casefold()
    for marker, city in VENUE_CITIES.items():
        if marker in folded:
            return city
    return ''


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.event-banner__event-title'))
    venue = clean_text(soup.select_one('.event-banner__event-location'))
    if re.search(r'local\s+(?:a\s+)?anunciar|local\s+a\s+definir', venue, re.I):
        return None
    event_date, time_from = parse_date_time(soup)
    city = city_from_venue(venue)
    description = clean_text(soup.select_one('.content.wysiwyg')) or None
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'PT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    session = make_session()
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, canonical_url(response.url))


class MetropolitanaPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='metropolitana_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        urls = sitemap_urls(make_session())
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    MetropolitanaPtCrawler().run()


if __name__ == '__main__':
    main()
