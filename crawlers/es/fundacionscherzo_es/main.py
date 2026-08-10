import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fundacionscherzo.es/'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Fundación Scherzo'
CITY = 'Madrid'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}
DATE_RE = re.compile(
    r'(?P<day>\d{1,2})\s+de\s+(?P<month>' + '|'.join(MONTHS) +
    r')\s+de\s+(?P<year>\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)')


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def get_pages(session):
    response = session.get(
        PAGES_API,
        params={
            'per_page': 100,
            '_fields': 'id,link,title,content,parent',
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def parse_date(match):
    try:
        return date(
            int(match.group('year')),
            MONTHS[match.group('month').lower()],
            int(match.group('day')),
        ).isoformat()
    except ValueError:
        return None


def resolve_venue(text_after_date):
    # Event pages consistently put the venue between the date and programme.
    # Some older entries abbreviate Auditorio Nacional to the room alone.
    head = re.split(r'\b(?:programa|comprar entradas|biograf[ií]a)\b', text_after_date,
                    maxsplit=1, flags=re.IGNORECASE)[0][:300]
    normalized = re.sub(r'\s+', ' ', head).strip(' .,-')

    centro = re.search(
        r'Auditorio\s+Centro\s*Centro(?:\s*\([^)]*\))?', normalized,
        re.IGNORECASE,
    )
    if centro:
        return clean_text(centro.group(0))

    nacional = re.search(
        r'Auditorio\s+Nacional(?:\s+de\s+M[uú]sica)?'
        r'(?:\s*[.·,-]\s*Sala\s+(?:Sinf[oó]nica|de\s+C[aá]mara))?',
        normalized,
        re.IGNORECASE,
    )
    if nacional:
        venue = clean_text(nacional.group(0)).rstrip(' .,-')
        return venue if re.search(r'Sala', venue, re.IGNORECASE) else 'Auditorio Nacional de Música'

    room = re.search(r'\bSala\s+(Sinf[oó]nica|de\s+C[aá]mara)\b', normalized,
                     re.IGNORECASE)
    if room:
        return f'Auditorio Nacional de Música. {clean_text(room.group(0))}'
    return None


def extract_description(content):
    text = clean_text(content, separator='\n')
    match = re.search(r'(?:^|\n)\s*programa\s*(?:\n|:)', text, re.IGNORECASE)
    if not match:
        return None
    description = text[match.start():]
    description = re.split(
        r'\n\s*(?:COMPRAR\s+ENTRADAS|DESCARGAR\s+PROGRAMA|BIOGRAF[IÍ]A)\s*(?:\n|$)',
        description,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return description.strip() or None


def make_record(page):
    title = clean_text((page.get('title') or {}).get('rendered'))
    content = (page.get('content') or {}).get('rendered') or ''
    text = clean_text(content)
    date_matches = list(DATE_RE.finditer(text))

    # Detail pages lead with one concert date. Calendar/index pages contain
    # many dates, while legal prose can contain an incidental date much later.
    if (not title or title.lower() == 'ficha intérprete' or not date_matches or
            date_matches[0].start() > 250):
        return None
    first_date = parse_date(date_matches[0])
    if not first_date:
        return None

    after_date = text[date_matches[0].end():]
    venue = resolve_venue(after_date)
    if not venue:
        return None
    time_match = TIME_RE.search(after_date[:300])
    url = page.get('link') or ''
    if not url:
        return None

    return {
        'title': title,
        'date': first_date,
        'url': url,
        'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        'venue': venue,
        'city': CITY,
        'country_code': 'ES',
        'description': extract_description(content),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    pages = get_pages(session)
    records = []
    for page in pages:
        try:
            record = make_record(page)
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse concert page',
                event='crawler_item_failed',
                level='warning',
                url=page.get('link'),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    # Occasionally an event has both an old draft-like detail page and its
    # published page. At this source simultaneous concerts are not offered;
    # retain the datetime variant carrying the richer programme.
    unique = {}
    for record in records:
        key = (record['date'], record['time_from'])
        previous = unique.get(key)
        if previous is None or len(record['description'] or '') > len(previous['description'] or ''):
            unique[key] = record

    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FundacionScherzoEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fundacionscherzo_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
    FundacionScherzoEsCrawler().run()


if __name__ == '__main__':
    main()
