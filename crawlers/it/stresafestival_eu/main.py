import re
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.stresafestival.eu/'
SITEMAP_URL = f'{SOURCE_URL}portfolio-item-sitemap.xml'
SOURCE = 'Stresa Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}
MONTH_PATTERN = '|'.join(MONTHS)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    parser = 'xml' if url.endswith('.xml') else 'html.parser'
    return BeautifulSoup(response.content, parser)


def event_urls(soup):
    urls = []
    for node in soup.select('loc') or soup.select('a[href]'):
        url = clean_text(node) if node.name == 'loc' else node.get('href', '')
        path = urlparse(url).path
        if path.startswith('/festival/') and path != '/festival/' and url not in urls:
            urls.append(url)
    return urls


def parse_dates(value):
    dates = []
    pattern = re.compile(
        rf'\b(\d{{1,2}})(?:\s*(?:\n|e)\s*(?:[A-Za-zÀ-ÿ]+\s+)?(\d{{1,2}}))?'
        rf'\s+({MONTH_PATTERN})\s+(20\d{{2}})\b',
        re.IGNORECASE,
    )
    for match in pattern.finditer(value):
        month = MONTHS[match.group(3).casefold()]
        for day_text in (match.group(1), match.group(2)):
            if not day_text:
                continue
            try:
                parsed = date(int(match.group(4)), month, int(day_text)).isoformat()
            except ValueError:
                continue
            if parsed not in dates:
                dates.append(parsed)
    return dates


def parse_city(location_text):
    postcode = re.search(r'\b\d{5}\s+([^\n,(]+)', location_text)
    if postcode:
        return postcode.group(1).strip()

    province = re.search(r'(?:–|-|,)\s*([^\n,(]+)\s*\([A-Z]{2}\)\b', location_text)
    if province:
        return province.group(1).strip()

    known = re.search(
        r'\b(Stresa|Milano|Angera|Arona|Leggiuno|Verbania|Baveno|Orta San Giulio)\b',
        location_text,
        re.IGNORECASE,
    )
    return known.group(1).strip() if known else None


def parse_detail(soup, url):
    content = soup.select_one('.qodef-portfolio-single-item') or soup.select_one('main')
    title_node = content.select_one('h1') if content else None
    if content is None or title_node is None:
        return []

    title = clean_text(title_node)
    full_text = clean_text(content)
    when_match = re.search(r'QUANDO E DOVE\s*(.*?)(?:\n\s*BIGLIETTI\b|\Z)', full_text, re.S | re.I)
    if not title or not when_match:
        return []

    when_text = clean_text(when_match.group(1))
    dates = parse_dates(when_text)
    time_match = re.search(r'\bore\s+(\d{1,2}):(\d{1,2})\b', when_text, re.I)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23 and int(time_match.group(2)) <= 59:
        time_from = f'{int(time_match.group(1)):02d}:{int(time_match.group(2)):02d}'

    location_text = re.sub(r'^.*?\bore\s+\d{1,2}:\d{1,2}\s*', '', when_text, count=1, flags=re.S | re.I)
    location_text = re.split(r'\n(?:Come raggiungerci|►)', location_text, maxsplit=1, flags=re.I)[0]
    lines = [line.strip() for line in location_text.splitlines() if line.strip()]
    city = parse_city(location_text)
    venue_lines = []
    for line in lines:
        if re.search(r'\b\d{5}\b|\b(?:via|viale|piazza|piazzale|corso|lungolago)\b', line, re.I):
            break
        venue_lines.append(line)
    venue = clean_text('\n'.join(venue_lines))
    if not dates or not city or not venue:
        return []

    description = re.split(r'\n\s*ARTISTI\b', full_text, maxsplit=1, flags=re.I)[0]
    description = re.sub(rf'^\s*{re.escape(title)}\s*', '', description, count=1, flags=re.I)
    description = clean_text(description) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'IT',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class StresaFestivalEuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stresafestival_eu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = event_urls(get_soup(session, SITEMAP_URL))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Stresa Festival sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                records.extend(parse_detail(get_soup(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Stresa Festival item',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    StresaFestivalEuCrawler().run()


if __name__ == '__main__':
    main()
