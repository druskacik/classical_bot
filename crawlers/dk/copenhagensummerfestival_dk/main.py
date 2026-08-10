import re
import time
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.copenhagensummerfestival.dk/'
SOURCE = 'Copenhagen Summer Festival'
DEFAULT_VENUE = 'Charlottenborg Festsal'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'marts': 3, 'april': 4, 'maj': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'december': 12,
}

DATE_RE = re.compile(
    r'(?P<day>\d{1,2})\.?(?:\s+|\s*[,/-]\s*)'
    r'(?P<month>januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december)'
    r'.{0,18}?(?:kl\.?\s*)?(?P<hour>[0-2]?\d)[.:](?P<minute>[0-5]\d)',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def cache_busted(url):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['_cfetch'] = str(time.time_ns())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def page_year(url, soup):
    match = re.search(r'(20\d{2})', url)
    if match:
        return int(match.group(1))
    match = re.search(r'\b(20\d{2})\b', clean_text(soup.select_one('main')))
    return int(match.group(1)) if match else None


def parse_datetime(text, year):
    match = DATE_RE.search(text)
    if not match or year is None:
        return None
    try:
        event_date = date(
            year, MONTHS[match.group('month').casefold()], int(match.group('day'))
        ).isoformat()
    except ValueError:
        return None
    event_time = f"{int(match.group('hour')):02d}:{match.group('minute')}"
    return event_date, event_time


def venue_for(text):
    lowered = text.casefold()
    if 'christians kirke' in lowered:
        return 'Christians Kirke'
    return DEFAULT_VENUE


def event_url(container, page_url):
    anchors = container.select('a[href]')
    links = [urljoin(page_url, link.get('href', '')) for link in anchors]
    for anchor, link in zip(anchors, links):
        if 'program' in clean_text(anchor).casefold() and '.pdf' in link.lower():
            return link.split('?', 1)[0]
    for link in links:
        if 'ticketmaster.dk/event/' in link:
            return link
    return page_url


def make_record(title, event_date, event_time, venue, description, url):
    title = clean_text(title)
    if not title:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': 'Copenhagen',
        'country_code': 'DK',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_cards(soup, page_url, year):
    records = []
    date_nodes = [
        node for node in soup.select('main h1, main h2, main h3, main h4')
        if parse_datetime(clean_text(node), year)
    ]
    for node in date_nodes:
        container = node.find_parent(class_='panel-grid-cell') or node.parent
        text = clean_text(container)
        parsed = parse_datetime(clean_text(node), year)
        content = node.find_next_sibling()
        content_text = clean_text(content)
        lines = [line for line in content_text.splitlines() if line]
        ignored_titles = {
            SOURCE.casefold(), 'charlottenborg festsal', 'christians kirke',
        }
        title_index = next(
            (index for index, line in enumerate(lines)
             if not line.casefold().startswith('foto:')
             and line.casefold() not in ignored_titles),
            None,
        )
        title = lines[title_index] if title_index is not None else ''
        if title.endswith('&') and title_index + 1 < len(lines):
            title = f'{title} {lines[title_index + 1]}'
        if not title:
            continue
        record = make_record(
            title, parsed[0], parsed[1], venue_for(text), content_text,
            event_url(container, page_url),
        )
        if record:
            records.append(record)
    return records


def parse_legacy(soup, page_url, year):
    main_text = clean_text(soup.select_one('main'))
    matches = list(DATE_RE.finditer(main_text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(main_text)
        chunk = main_text[match.start():end].strip()
        parsed = parse_datetime(chunk, year)
        remainder = chunk[DATE_RE.search(chunk).end():].strip()
        lines = [line.strip(' -*') for line in remainder.splitlines() if line.strip(' -*')]
        title = lines[0] if lines else ''
        record = make_record(
            title, parsed[0], parsed[1], venue_for(chunk), chunk, page_url
        )
        if record:
            records.append(record)
    return records


class CopenhagenSummerFestivalDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='copenhagensummerfestival_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
        upload_target='classical',
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
            response = session.get(cache_busted(SOURCE_URL), timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Copenhagen Summer Festival',
                event='crawler_fetch_failed', level='error', url=SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        home_soup = BeautifulSoup(response.text, 'html.parser')
        page_urls = [SOURCE_URL]
        for link in home_soup.select('a[href*="/historie/program-"]'):
            url = urljoin(SOURCE_URL, link.get('href', '')).split('#', 1)[0]
            if url not in page_urls:
                page_urls.append(url)

        records = []
        for page_url in page_urls:
            try:
                if page_url == SOURCE_URL:
                    soup = home_soup
                else:
                    page_response = session.get(cache_busted(page_url), timeout=45)
                    page_response.raise_for_status()
                    soup = BeautifulSoup(page_response.text, 'html.parser')
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch festival archive page',
                    event='crawler_item_fetch_failed', level='warning', url=page_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            year = page_year(page_url, soup)
            page_records = parse_cards(soup, page_url, year)
            records.extend(
                page_records if len(page_records) > 1 else parse_legacy(soup, page_url, year)
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CopenhagenSummerFestivalDkCrawler().run()


if __name__ == '__main__':
    main()
