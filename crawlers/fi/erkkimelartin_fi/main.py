import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://erkkimelartin.fi/em/'
SOURCE = 'Erkki Melartin -seura'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# The site is a news archive rather than an event database.  These first-party
# venue names occur in its concert notices and give a defensible city without
# mistaking an address, performer, or ticket outlet for a location.
VENUES = {
    'Heikki Sarvela-sali': 'Liminka',
    'Pohjanrannan juhlahuoneisto': 'Keminmaa',
    'Oulun Lyseon juhlasali': 'Oulu',
    'Lauttasaaren kirkko': 'Helsinki',
    'Paavalinkirkko': 'Helsinki',
    'Paavalin kirkko': 'Helsinki',
    'Paavo-sali': 'Helsinki',
    'Musiikkitalon Paavo-sali': 'Helsinki',
    'Ruusu-Ristin sali': 'Helsinki',
    'Pasilan kirjaston auditorio': 'Helsinki',
    'Pasilan kirjasto': 'Helsinki',
    'Sibelius-Akatemia': 'Helsinki',
    'Turun tuomiokirkko': 'Turku',
    'Lallukan juhlasali': 'Helsinki',
    'Taidekoti Kirpilä': 'Helsinki',
    'Riihimäen taidemuseo': 'Riihimäki',
    'Kokkolan pääkirjasto': 'Kokkola',
}

EVENT_WORDS = re.compile(
    r'konsert|oopper|resitaal|musiikki-ilta|musiikkijuh|luentokonsert', re.I
)
DATE_RE = re.compile(
    r'(?<!\d)(?:([0-3]?\d)\s*\.\s*[-–]\s*)?'
    r'([0-3]?\d)\s*\.\s*([01]?\d)\s*\.\s*(20\d{2})?'
)
TIME_RE = re.compile(r'\bklo\s*(?:noin\s*)?([01]?\d|2[0-3])(?:[.:]([0-5]\d))?', re.I)


def clean_text(element):
    if not element:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_published(article):
    time = article.select_one('time[datetime]')
    if time:
        try:
            return datetime.fromisoformat(time.get('datetime').replace('Z', '+00:00')).date()
        except (TypeError, ValueError):
            pass
    return None


def nearby_location(text, match):
    start = max(0, match.start() - 160)
    end = min(len(text), match.end() + 220)
    context = text[start:end]
    found = []
    for venue, city in VENUES.items():
        pos = context.casefold().find(venue.casefold())
        if pos >= 0:
            absolute_pos = start + pos
            found.append((abs(absolute_pos - match.start()), -len(venue), venue, city))
    if not found:
        return None, None
    _, _, venue, city = min(found)
    return venue, city


def event_date(match, published):
    year = int(match.group(4)) if match.group(4) else (published.year if published else None)
    if year is None:
        return None
    try:
        value = date(year, int(match.group(3)), int(match.group(2)))
    except ValueError:
        return None
    # A yearless notice late in the year may advertise the following January.
    if not match.group(4) and published and value < published - timedelta(days=90):
        try:
            value = value.replace(year=year + 1)
        except ValueError:
            return None
    return value


def nearby_time(text, match):
    context = text[match.start():min(len(text), match.end() + 80)]
    found = TIME_RE.search(context)
    if not found:
        context = text[max(0, match.start() - 45):match.start()]
        found = TIME_RE.search(context)
    if not found:
        return None
    return f'{int(found.group(1)):02d}:{found.group(2) or "00"}'


def parse_article(article, page_url):
    heading = article.select_one('.entry-title, h1, h2')
    link = heading.find('a', href=True) if heading else None
    content = article.select_one('.entry-content')
    title = clean_text(heading)
    description = clean_text(content)
    url = urljoin(page_url, link['href']) if link else page_url
    if not title or not description or not EVENT_WORDS.search(f'{title}\n{description}'):
        return []

    published = parse_published(article)
    records = []
    for match in DATE_RE.finditer(description):
        # Dates in parenthesized author/publication citations are not concert
        # occurrences (for example “(Henrik Järvi, 19.7.2026 Amfion)”).
        if re.search(r'\([^)]*,\s*$', description[max(0, match.start() - 100):match.start()]):
            continue
        parsed_date = event_date(match, published)
        venue, city = nearby_location(description, match)
        if not parsed_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': parsed_date.isoformat(),
            'url': url,
            'time_from': nearby_time(description, match),
            'venue': venue,
            'city': city,
            'country_code': 'FI',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        if match.group(1):
            try:
                range_start = parsed_date.replace(day=int(match.group(1)))
            except ValueError:
                continue
            records.append({**records[-1], 'date': range_start.isoformat()})
    return records


class ErkkimelartinFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='erkkimelartin_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        first_soup = BeautifulSoup(response.text, 'html.parser')
        page_numbers = [
            int(text) for element in first_soup.select('.page-numbers')
            if (text := clean_text(element)).isdigit()
        ]
        last_page = max(page_numbers, default=1)

        pages = [(1, SOURCE_URL, first_soup)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(
                    requests.get,
                    f'{SOURCE_URL}?paged={page_number}',
                    headers=HEADERS,
                    timeout=45,
                ): page_number
                for page_number in range(2, last_page + 1)
            }
            for future in as_completed(futures):
                page_number = futures[future]
                page_url = f'{SOURCE_URL}?paged={page_number}'
                try:
                    response = future.result()
                    response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Erkki Melartin archive page',
                        event='crawler_page_failed',
                        level='warning',
                        url=page_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                pages.append((page_number, page_url, BeautifulSoup(response.text, 'html.parser')))

        for _, page_url, soup in sorted(pages):
            articles = soup.select('main article, #main article')
            for article in articles:
                records.extend(parse_article(article, page_url))

        log_message(
            'Erkki Melartin archive scrape completed',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    ErkkimelartinFiCrawler().run()


if __name__ == '__main__':
    main()
