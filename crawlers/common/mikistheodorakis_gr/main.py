import re
import unicodedata
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mikistheodorakis.gr/'
EVENTS_URL = urljoin(SOURCE_URL, 'el/events/')
SOURCE = 'Μίκης Θεοδωράκης'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'el-GR,el;q=0.9,en;q=0.7',
}

MONTHS = {
    'ιανουαριου': 1, 'φεβρουαριου': 2, 'μαρτιου': 3, 'απριλιου': 4,
    'μαιου': 5, 'ιουνιου': 6, 'ιουλιου': 7, 'αυγουστου': 8,
    'σεπτεμβριου': 9, 'οκτωβριου': 10, 'νοεμβριου': 11, 'δεκεμβριου': 12,
}

# The archive often supplies a hall rather than a postal address. These are
# first-party names found in the event copy and are only used on exact matches.
VENUES = [
    (('κηπο του μεγαρου', 'κηπο μεγαρου'), 'Κήπος του Μεγάρου Μουσικής Αθηνών', 'Athens', 'GR'),
    (('μεγαρο μουσικης αθηνων', 'αιθουσα χρηστος λαμπρακης', 'στο μεγαρο'), 'Μέγαρο Μουσικής Αθηνών', 'Athens', 'GR'),
    (('καλλιμαρμαρο', 'παναθηναικο σταδιο'), 'Παναθηναϊκό Στάδιο', 'Athens', 'GR'),
    (('ωδειο ηρωδου του αττικου', 'ηρωδειο'), 'Ωδείο Ηρώδου του Αττικού', 'Athens', 'GR'),
    (('μπουατ απανεμια', 'απανεμια'), 'Μπουάτ Απανεμιά', 'Athens', 'GR'),
    (('φιλολογικο συλλογο παρνασσος', 'παρνασσο'), 'Φιλολογικός Σύλλογος Παρνασσός', 'Athens', 'GR'),
    (('μουσικη σκηνη σφιγγα', 'στην σφιγγα'), 'Μουσική Σκηνή Σφίγγα', 'Athens', 'GR'),
    (('στον ιανο', 'στο ιανο'), 'ΙΑΝΟΣ', 'Athens', 'GR'),
    (('θεατρο badminton',), 'Θέατρο Badminton', 'Athens', 'GR'),
    (('κεντρικη πλατεια νεας σμυρνης',), 'Κεντρική Πλατεία Νέας Σμύρνης', 'Nea Smyrni', 'GR'),
    (('δημοτικο θεατρο απολλων',), 'Δημοτικό Θέατρο Απόλλων', 'Pyrgos', 'GR'),
    (('αρχαιο θεατρο του διου',), 'Αρχαίο Θέατρο Δίου', 'Dion', 'GR'),
    (('στη μακρονησο', 'στην μακρονησο'), 'Μακρόνησος', 'Makronisos', 'GR'),
    (('παττιχειο αμφιθεατρο',), 'Παττίχειο Αμφιθέατρο', 'Larnaca', 'CY'),
    (('κηποθεατρο λεμεσου',), 'Κηποθέατρο Λεμεσού', 'Limassol', 'CY'),
    (('tonhalle',), 'Tonhalle Düsseldorf', 'Düsseldorf', 'DE'),
    (('βιεννη', 'vienna'), 'Wiener Konzerthaus', 'Vienna', 'AT'),
    (('εθνικη λυρικη σκηνη στο κλουι', 'κλουι-ναποτσα'), 'Opera Națională Română Cluj-Napoca', 'Cluj-Napoca', 'RO'),
]


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFD', value.lower())
    return ''.join(char for char in value if unicodedata.category(char) != 'Mn')


def publication_date(value):
    match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(20\d{2})', value.strip())
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None


def _date_candidates(text, published):
    """Return plausible performance dates near the article's publication."""
    sample = normalized(text)
    found = set()
    for match in re.finditer(r'\b(\d{1,2})[./-](\d{1,2})(?:[./-](20\d{2}))?\b', sample):
        year = int(match.group(3)) if match.group(3) else published.year
        try:
            found.add(date(year, int(match.group(2)), int(match.group(1))))
        except ValueError:
            continue
    month_names = '|'.join(MONTHS)
    for match in re.finditer(rf'\b(\d{{1,2}})\s+({month_names})(?:\s+(20\d{{2}}))?\b', sample):
        year = int(match.group(3)) if match.group(3) else published.year
        try:
            found.add(date(year, MONTHS[match.group(2)], int(match.group(1))))
        except ValueError:
            continue

    lower = published - timedelta(days=2)
    upper = published + timedelta(days=400)
    return sorted(candidate for candidate in found if lower <= candidate <= upper)


def event_dates(title, body, published):
    # A date in the headline is unambiguously about the advertised occurrence.
    # Otherwise use the opening copy only: later paragraphs frequently discuss
    # composition dates and historic premieres.
    title_dates = _date_candidates(title, published)
    if title_dates:
        return title_dates
    dates = _date_candidates(body[:1000], published)
    if not dates:
        return []
    first = dates[0]
    return [candidate for candidate in dates if candidate <= first + timedelta(days=90)]


def event_time(text):
    sample = normalized(text[:1200])
    patterns = (
        r'(?:ωρα|ωρα εναρξης|στις)\s*[:]?\s*([012]?\d)[.:]([0-5]\d)',
        r'\b([012]?\d)[.:]([0-5]\d)\s*(?:μ\.?μ|μμ|το βραδυ)',
    )
    for pattern in patterns:
        match = re.search(pattern, sample)
        if match:
            hour = int(match.group(1))
            suffix = sample[match.start():match.end() + 24]
            if ('μ.μ' in suffix or 'μμ' in suffix or 'βραδυ' in suffix) and hour < 12:
                hour += 12
            if hour < 24:
                return f'{hour:02d}:{int(match.group(2)):02d}'
    return None


def location(text):
    haystack = normalized(text)
    matches = []
    for aliases, venue, city, country_code in VENUES:
        if any(normalized(alias) in haystack for alias in aliases):
            matches.append((venue, city, country_code))
    unique = list(dict.fromkeys(matches))
    if unique and unique[0][0] == 'Κήπος του Μεγάρου Μουσικής Αθηνών':
        return unique[0]
    return unique[0] if len(unique) == 1 else None


def detail_record(session, item):
    url = urljoin(EVENTS_URL, item.get('data-link') or '')
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('.articleheader h1')) or clean_text(item.select_one('h2'))
    published = publication_date(clean_text(soup.select_one('.articleheader h2')))
    body = clean_text(soup.select_one('.anarticle article'))
    if not title or not published or not body:
        return []

    combined = f'{title}\n{body}'
    resolved_location = location(combined)
    dates = event_dates(title, body, published)
    if not dates and 'μεγαλη παρασκευη' in normalized(title):
        dates = [published]
    if not resolved_location or not dates:
        return []
    venue, city, country_code = resolved_location
    time_from = event_time(combined)
    return [{
        'title': title,
        'date': event_date.isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': body,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


class MikisTheodorakisGrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mikistheodorakis_gr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        records = []
        offset = 0
        seen_urls = set()

        while True:
            try:
                response = session.get(
                    EVENTS_URL,
                    params={'st': offset} if offset else None,
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Mikis Theodorakis event listing',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else EVENTS_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            items = soup.select('ul#news > li[data-link*="nid="]')
            new_items = []
            for item in items:
                url = urljoin(EVENTS_URL, item.get('data-link'))
                if url not in seen_urls:
                    seen_urls.add(url)
                    new_items.append(item)
            if not new_items:
                break

            for item in new_items:
                url = urljoin(EVENTS_URL, item.get('data-link'))
                try:
                    records.extend(detail_record(session, item))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Mikis Theodorakis event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

            next_offsets = []
            for link in soup.select('a.pageslink[href*="st="]'):
                match = re.search(r'[?&]st=(\d+)', link.get('href', ''))
                if match and int(match.group(1)) > offset:
                    next_offsets.append(int(match.group(1)))
            if not next_offsets:
                break
            offset = min(next_offsets)

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    MikisTheodorakisGrCrawler().run()


if __name__ == '__main__':
    main()
