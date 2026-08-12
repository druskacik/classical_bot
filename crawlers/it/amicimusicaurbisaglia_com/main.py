import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://amicimusicaurbisaglia.com/'
SOURCE = 'Amici della Musica Urbisaglia'
API_URL = (
    'https://public-api.wordpress.com/wp/v2/sites/'
    'amicimusicaurbisaglia.com/pages'
)
ARCHIVE_SLUG = 'archivio-concerti'
UPCOMING_SLUG = 'prossimi-concerti'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1,
    'febbraio': 2,
    'marzo': 3,
    'aprile': 4,
    'maggio': 5,
    'giugno': 6,
    'luglio': 7,
    'agosto': 8,
    'settembre': 9,
    'ottobre': 10,
    'novembre': 11,
    'dicembre': 12,
}

DATE_RE = re.compile(
    r'\b(?:lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|'
    r'giovedi|giovedì|venerdi|venerdì|sabato|domenica)?\s*'
    r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})\b',
    re.IGNORECASE,
)
DATE_WITHOUT_YEAR_RE = re.compile(
    r'\b(?:lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|'
    r'giovedi|giovedì|venerdi|venerdì|sabato|domenica)?\s*'
    r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(?:ore|h)\s*(\d{1,2})(?:[.:](\d{2}))?\b', re.IGNORECASE)

# The archive frequently omits a separate address field. These are venue/city
# pairs explicitly used in its own rows; matching the venue prevents a city
# name alone from becoming a placeholder venue.
LOCATION_RULES = [
    (r'Giardino Palazzo Giustiniani Bandini', 'Giardino Palazzo Giustiniani Bandini', 'Tolentino'),
    (r'Chiesa di San Nicol[oò]', 'Chiesa di San Nicolò', 'Jesi'),
    (r'Auditorium Naturale Abbadia di Fiastra', 'Auditorium Naturale Abbadia di Fiastra', 'Tolentino'),
    (r'Abbazia di Chiaravalle di Fiastra', 'Abbazia di Chiaravalle di Fiastra', 'Tolentino'),
    (r'Villa Spada(?: di Treia)?', 'Villa Spada', 'Treia'),
    (r'Anfiteatro Romano(?: di Urbisaglia)?', 'Anfiteatro Romano', 'Urbisaglia'),
    (r'Giardino Palazzo Giustiniani Bandini', 'Giardino Palazzo Giustiniani Bandini', 'Tolentino'),
    (r'Teatro Comunale(?:\s+di|\s*[,–-])\s*Loro Piceno', 'Teatro Comunale', 'Loro Piceno'),
    (r'Bar Teatro Villa Potenza', 'Bar Teatro Villa Potenza', 'Macerata'),
    (r'Auditorium Bocelli', 'Auditorium Bocelli', 'Camerino'),
    (r'Cortile della Biblioteca Filelfica', 'Cortile della Biblioteca Filelfica', 'Tolentino'),
    (r'Chiesa di Santa Maria', 'Chiesa di Santa Maria', 'Loro Piceno'),
    (r'Teatro Filarmonica', 'Teatro Filarmonica', 'Macerata'),
    (r'Palazzo Mercantile', 'Palazzo Mercantile', 'Bolzano'),
    (r'Arena Gigli', 'Arena Gigli', 'Porto Recanati'),
    (r'Teatro dell[\u2019\']Aquila', 'Teatro dell’Aquila', 'Fermo'),
    (r'Gran Sala Cesanelli', 'Gran Sala Cesanelli', 'Macerata'),
    (r'Collegiata di San Lorenzo', 'Collegiata di San Lorenzo', 'Urbisaglia'),
    (r'Chiesa della Maest[aà]', 'Chiesa della Maestà', 'Urbisaglia'),
    (r'Aula Verde', 'Aula Verde', 'Tolentino'),
    (r'Giardino Forconi', 'Giardino Forconi', 'Urbisaglia'),
    (r'Teatro della Vittoria', 'Teatro della Vittoria', 'Urbisaglia'),
    (r'Teatro Comunale', 'Teatro Comunale', 'Urbisaglia'),
]


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text, fallback_year=None):
    match = DATE_RE.search(text)
    if match:
        day, month, year = match.groups()
    else:
        match = DATE_WITHOUT_YEAR_RE.search(text)
        if not match or fallback_year is None:
            return None
        day, month = match.groups()
        year = fallback_year
    try:
        return date(int(year), MONTHS[month.casefold()], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or '00')
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def resolve_location(text):
    for pattern, venue, city in LOCATION_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return venue, city
    return None


def title_from_cell(cell, text):
    # Linked titles are the most precise signal in the archive. Ignore links to
    # video evidence only when there is a better strong-labelled title.
    links = [clean_text(link) for link in cell.select('a') if clean_text(link)]
    if links:
        return re.sub(r'\s+', ' ', links[0])

    strong_text = clean_text('\n'.join(clean_text(node) for node in cell.select('strong')))
    candidates = strong_text.splitlines() + text.splitlines()
    for candidate in candidates:
        candidate = DATE_RE.sub('', candidate).strip(' .–—-')
        candidate = DATE_WITHOUT_YEAR_RE.sub('', candidate)
        candidate = TIME_RE.sub('', candidate).strip(' .–—-')
        if re.fullmatch(
            r'(?:lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|'
            r'giovedi|giovedì|venerdi|venerdì|sabato|domenica)[’\']?',
            candidate,
            re.IGNORECASE,
        ):
            continue
        if not candidate:
            continue
        # Some compact historical rows put the title and location in one line.
        venue_positions = [
            match.start()
            for pattern, _, _ in LOCATION_RULES
            if (match := re.search(pattern, candidate, re.IGNORECASE))
        ]
        if venue_positions:
            candidate = candidate[:min(venue_positions)].strip(' .–—-')
        candidate = re.sub(
            r'\s+[–—-]\s+Loro Piceno\s*,?\s*$',
            '',
            candidate,
            flags=re.IGNORECASE,
        ).strip(' .–—-')
        candidate = re.sub(r'\s+', ' ', candidate)
        if candidate:
            return candidate
    return None


def parse_archive_cell(cell, page_url):
    text = clean_text(cell)
    event_date = parse_date(text)
    location = resolve_location(text)
    title = title_from_cell(cell, text)
    if not event_date or not location or not title:
        return None

    detail_link = next(
        (
            link.get('href')
            for link in cell.select('a[href]')
            if link.get('href', '').startswith(SOURCE_URL)
        ),
        None,
    )
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': detail_link or page_url,
        'time_from': parse_time(text),
        'venue': venue,
        'city': city,
        'description': text or None,
    }


def fetch_page(session, slug):
    response = session.get(
        API_URL,
        params={
            'slug': slug,
            '_fields': 'link,modified,content',
            'per_page': 1,
        },
        timeout=45,
    )
    response.raise_for_status()
    pages = response.json()
    if not pages:
        raise ValueError(f'WordPress page not found: {slug}')
    return pages[0]


class AmicimusicaurbisagliaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicimusicaurbisaglia_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            archive_page = fetch_page(session, ARCHIVE_SLUG)
            upcoming_page = fetch_page(session, UPCOMING_SLUG)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Amici della Musica Urbisaglia archive',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(
            archive_page.get('content', {}).get('rendered', ''),
            'html.parser',
        )
        records = []
        for cell in soup.select('table tr > td'):
            record = parse_archive_cell(cell, archive_page.get('link') or SOURCE_URL)
            if record:
                records.append(record)

        upcoming_soup = BeautifulSoup(
            upcoming_page.get('content', {}).get('rendered', ''),
            'html.parser',
        )
        modified = upcoming_page.get('modified', '')
        fallback_year = int(modified[:4]) if re.match(r'^\d{4}', modified) else None
        for paragraph in upcoming_soup.select('p'):
            text = clean_text(paragraph)
            event_date = parse_date(text, fallback_year=fallback_year)
            location = resolve_location(text)
            title = title_from_cell(paragraph, text)
            if not event_date or not location or not title:
                continue
            venue, city = location
            records.append({
                'title': title,
                'date': event_date,
                'url': upcoming_page.get('link') or SOURCE_URL,
                'time_from': parse_time(text),
                'venue': venue,
                'city': city,
                'description': text or None,
            })
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AmicimusicaurbisagliaComCrawler().run()


if __name__ == '__main__':
    main()
