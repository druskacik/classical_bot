import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatrocomunalemodena.it/'
ARCHIVE_URL = f'{SOURCE_URL}archivio-spettacoli/'
SOURCE = 'Teatro Comunale di Modena Pavarotti-Freni'
DEFAULT_VENUE = 'Teatro Comunale Pavarotti-Freni'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

DATE_RE = re.compile(
    r'\b(\d{1,2})\s+'
    r'(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)'
    r'\s+(20\d{2})(?:\s*(?:[-–]|ore)?\s*(\d{1,2})[.:](\d{2}))?',
    re.I,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def archive_urls(soup):
    urls = []
    for link in soup.select('.archivio-post-title a[href*="/spettacolo/"]'):
        url = link.get('href', '').split('#', 1)[0]
        if url and url not in urls:
            urls.append(url)
    return urls


def event_dates(soup):
    heading = soup.select_one('h1')
    if heading is None:
        return []
    hero = heading.find_next('h4')
    if hero is None:
        return []
    results = []
    for match in DATE_RE.finditer(clean_text(hero)):
        try:
            event_date = date(
                int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
            ).isoformat()
        except ValueError:
            continue
        time_from = None
        if match.group(4) and 0 <= int(match.group(4)) <= 23 and 0 <= int(match.group(5)) <= 59:
            time_from = f'{int(match.group(4)):02d}:{int(match.group(5)):02d}'
        results.append((event_date, time_from))
    return results


def location(soup, title):
    # The event template's left information column places an optional location
    # between the category and the repeated date. An empty location means the
    # performance is at this venue; named off-site locations must be explicit.
    for heading in soup.select('h4'):
        if clean_text(heading) != title:
            continue
        column = heading.find_parent(class_=lambda value: value and 'vc_col-sm-4' in value)
        if column is None:
            continue
        lines = [line.strip() for line in clean_text(column).splitlines() if line.strip()]
        candidates = [
            line for line in lines
            if line != title
            and not DATE_RE.search(line)
            and line.casefold() not in {'news', 'programma'}
        ]
        if not candidates:
            return DEFAULT_VENUE, 'Modena'

        value = candidates[0]
        if value.casefold() in {'teatro comunale', DEFAULT_VENUE.casefold()}:
            return DEFAULT_VENUE, 'Modena'
        if value.casefold() == 'modena':
            return None

        # Most off-site entries use "Venue, City" or "Venue - City". Do not
        # turn a bare municipality (for example Montecreto) into a venue.
        parts = [part.strip() for part in re.split(r'\s*[|,–]\s*|\s+-\s+', value) if part.strip()]
        if len(parts) >= 2:
            venue, city = parts[0], parts[-1]
            if venue.casefold() != city.casefold():
                return venue, city
        if re.search(r'\b(teatro|chiesa|auditorium|sala|palazzo|cortile|parco|chiostro|basilica)\b', value, re.I):
            return value, 'Modena'
        return None
    return DEFAULT_VENUE, 'Modena'


def description(soup, title):
    chunks = []
    for node in soup.select('#ajax-content-wrap .wpb_text_column'):
        text = clean_text(node)
        if not text or text == title or DATE_RE.fullmatch(text):
            continue
        folded = text.casefold()
        if folded in {'news', 'programma'} or folded.startswith(('biglietti', 'info e biglietti')):
            continue
        if node.find('table') or '€' in text:
            continue
        if text not in chunks:
            chunks.append(text)
    return clean_text('\n\n'.join(chunks)) or None


def parse_detail(soup, url):
    title = clean_text(soup.select_one('h1'))
    dates = event_dates(soup)
    place = location(soup, title) if title else None
    if not title or not dates or place is None:
        return []
    venue, city = place
    body = description(soup, title)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'IT',
            'description': body,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in dates
    ]


class TeatroComunaleModenaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrocomunalemodena_it',
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
        try:
            urls = archive_urls(get_soup(ARCHIVE_URL))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Teatro Comunale Modena archive',
                event='crawler_fetch_failed', level='error', url=ARCHIVE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(get_soup, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result(), url))
                except (requests.RequestException, TypeError, ValueError) as error:
                    log_message(
                        'Failed to parse Teatro Comunale Modena event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    TeatroComunaleModenaItCrawler().run()


if __name__ == '__main__':
    main()
