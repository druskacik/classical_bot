import re
import unicodedata
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.classiquicime-megeve.com/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programmation')
ARCHIVE_URL = urljoin(SOURCE_URL, 'premiere-edition')
SOURCE = 'Festival Classiquicime Megève'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}

# The two action-culture pages are conversations/education activities, not
# advertised public performances. All linked concert and musical-brunch pages
# are retained, including the archived first edition.
EXCLUDED_PATHS = {'/cafe-musique', '/musique-pour-tous'}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def folded(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') or '/', '', ''))


def discover_detail_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for link in soup.select('a[href]'):
        url = canonical_url(link.get('href'))
        parts = urlsplit(url)
        if parts.netloc != urlsplit(SOURCE_URL).netloc:
            continue
        if parts.path in EXCLUDED_PATHS or parts.path in {'/', '/programmation', '/premiere-edition'}:
            continue
        text = clean_text(link)
        if text == 'DÉCOUVRIR' or parts.path in {
            '/nuit-mozart', '/prelude-a-la-nuit-bach', '/nuit-du-piano-2025',
            '/brunch-musical-2025', '/prelude-a-la-nuit-quatuor-modigliani',
            '/rave-l-party', '/orchestre-harmonie-megeve',
        }:
            urls.add(url)
    return urls


def parse_dates(value):
    normalized = folded(clean_text(value))
    year_match = re.search(r'\b(20\d{2})\b', normalized)
    month_match = re.search(r'\b(' + '|'.join(MONTHS) + r')\b', normalized)
    if not year_match or not month_match:
        return []
    year = int(year_match.group(1))
    month = MONTHS[month_match.group(1)]
    day_text = normalized[:month_match.start()]
    days = [int(item) for item in re.findall(r'\b([0-3]?\d)\b', day_text)]
    results = []
    for day in days:
        try:
            value = date(year, month, day).isoformat()
        except ValueError:
            continue
        if value not in results:
            results.append(value)
    return results


def labelled_value(lines, label):
    pattern = re.compile(rf'^{label}\s*:\s*(.+)$', re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ''


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    lines = [clean_text(line) for line in soup.get_text('\n').splitlines() if clean_text(line)]
    date_text = labelled_value(lines, 'Date')
    venue = labelled_value(lines, 'Lieu')
    time_text = labelled_value(lines, 'Horaire')
    dates = parse_dates(date_text)

    title_meta = soup.select_one('meta[property="og:title"]')
    title = clean_text(title_meta.get('content')) if title_meta else ''
    title = re.split(r'\s*[|–-]\s*Festival Classiquicime', title, maxsplit=1)[0].strip()
    title = re.sub(r'\s+-\s+Programmation\s*$', '', title, flags=re.IGNORECASE)
    if title.startswith('Festival Classiquicime'):
        title = ''
    if not title:
        heading = next(
            (
                item for item in soup.select('h2')
                if clean_text(item)
                and len(clean_text(item)) <= 120
                and not clean_text(item).startswith('Festival Classiquicime')
                and 'Concert musique classique à Megève' not in clean_text(item)
                and 'Festival Classiquicime Megève :' not in clean_text(item)
                and clean_text(item) != 'Classiquicime'
            ),
            None,
        )
        title = clean_text(heading)

    # The 2025 closing-concert page predates the labelled detail template.
    if not dates:
        legacy_date = next(
            (line for line in lines if re.search(r'\b[0-3]?\d\s+mars\s+20\d{2}\b', folded(line))),
            '',
        )
        dates = parse_dates(legacy_date)
        if legacy_date and not time_text:
            time_text = legacy_date
        if dates and not venue:
            date_index = lines.index(legacy_date)
            venue = next(
                (line for line in lines[date_index + 1:date_index + 5] if 'eglise' in folded(line)),
                '',
            )

    time_match = re.search(r'\b([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)?\b', time_text)
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'

    description_start = next((index for index, line in enumerate(lines) if re.match(r'^Date\s*:', line, re.I)), None)
    description_end = next(
        (index for index, line in enumerate(lines) if line.startswith('Séjour et voyage musique classique')),
        len(lines),
    )
    description = None
    if description_start is not None:
        description = '\n'.join(lines[description_start:description_end]).strip() or None
    elif dates:
        description = '\n'.join(lines[:description_end]).strip() or None

    if urlsplit(url).path == '/concert-sur-les-cimes' and title and dates:
        return [
            {
                'title': title,
                'date': dates[0],
                'url': url,
                'time_from': event_time,
                'venue': event_venue,
                'city': 'Megève',
                'country_code': 'FR',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
            for event_time, event_venue in (
                ('12:00', 'Place du village de Megève'),
                ('13:30', 'Terrasse du Super Megève'),
                ('15:00', 'Terrasse du Super Megève'),
            )
        ]

    if not title or not dates or not venue:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Megève',
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class ClassiquicimeMegeveComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='classiquicime_megeve_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        detail_urls = set()
        for listing_url in (PROGRAMME_URL, ARCHIVE_URL):
            response = session.get(listing_url, timeout=45)
            response.raise_for_status()
            detail_urls.update(discover_detail_urls(response.text))

        records = []
        for url in sorted(detail_urls):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                parsed = parse_detail(response.text, url)
                if not parsed:
                    log_message(
                        'Skipped incomplete Classiquicime event page',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, or venue is missing',
                    )
                records.extend(parsed)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Classiquicime event page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ClassiquicimeMegeveComCrawler().run()


if __name__ == '__main__':
    main()
