import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sinfonicaciudaddezaragoza.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Sinfónica Ciudad de Zaragoza'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11,
    'diciembre': 12,
}

LOCATIONS = (
    (
        r'Auditorio de Zaragoza\s*\(Sala Mozart\)',
        'Auditorio de Zaragoza (Sala Mozart)',
        'Zaragoza',
    ),
    (
        r'Auditorio de Zaragoza\s*\(Sala Luis Galve\)',
        'Auditorio de Zaragoza (Sala Luis Galve)',
        'Zaragoza',
    ),
    (r'Teatro Principal de Zaragoza', 'Teatro Principal de Zaragoza', 'Zaragoza'),
    (r'Plaza San Bruno(?:\s*\(Zaragoza\))?', 'Plaza San Bruno', 'Zaragoza'),
    (r'Teatro de la Villa de Ejea de los Caballeros', 'Teatro de la Villa', 'Ejea de los Caballeros'),
    (r'Teatro Capitol de Calatayud', 'Teatro Capitol', 'Calatayud'),
    (r'Teatro Olimpia de Huesca', 'Teatro Olimpia', 'Huesca'),
    (r'Auditorio Baluarte de Pamplona', 'Auditorio Baluarte', 'Pamplona'),
    (r'Catedral de Pamplona', 'Catedral de Pamplona', 'Pamplona'),
    (r'Auditorio Municipal de Burlada', 'Auditorio Municipal de Burlada', 'Burlada'),
)

SECTION_RE = re.compile(
    r'\[vc_tta_section\s+(?P<attrs>[^\]]+)\](?P<body>.*?)\[/vc_tta_section\]',
    re.DOTALL,
)
DATE_RE = re.compile(
    r'(?<!\d)(?P<days>\d{1,2}(?:\s*,\s*\d{1,2})*'
    r'(?:\s+y\s+\d{1,2})?)\s+(?:de\s+)?'
    r'(?P<month>enero|febrero|marzo|abril|mayo|junio|julio|agosto|'
    r'septiembre|octubre|noviembre|diciembre)\b'
    r'(?:\s+(?:de\s+)?(?P<explicit_year>20\d{2}))?'
    r'(?:\s+(?P<time>[0-2]?\d[:.]\d{2})\s*h?\.?)?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'\[(?:/?vc_[^\]]+|/?vsc-[^\]]+)\]', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_content(session, slug):
    response = session.get(API_URL, params={'slug': slug}, timeout=60)
    response.raise_for_status()
    pages = response.json()
    if not pages:
        raise ValueError(f'WordPress page not found: {slug}')
    return pages[0].get('content', {}).get('rendered', '')


def attribute(attrs, name):
    match = re.search(rf'{name}=»(.*?)(?:″|»(?:\s+\w+=|$))', html.unescape(attrs))
    return clean_text(match.group(1)) if match else ''


def split_upcoming(content):
    """Split the responsive page layouts into event-shaped h3 blocks."""
    starts = list(re.finditer(r'<h3[^>]*>.*?</h3>', content, re.I | re.DOTALL))
    blocks = []
    for index, start in enumerate(starts):
        title = clean_text(start.group())
        end = starts[index + 1].start() if index + 1 < len(starts) else len(content)
        if title:
            blocks.append((title, content[start.end():end], ''))
    return blocks


def split_archive(content):
    blocks = []
    for match in SECTION_RE.finditer(content):
        title = attribute(match.group('attrs'), 'title')
        tab_id = attribute(match.group('attrs'), 'tab_id')
        blocks.append((title.split('|', 1)[0].strip(), match.group('body'), tab_id))
    return blocks


def event_year(title, body, month):
    heading = f'{title}\n{clean_text(body)[:300]}'
    years = re.findall(r'\b(20\d{2})\b', heading)
    if not years:
        return None
    year = int(years[0])
    normalized = heading.lower()
    if month == 12 and ('enero' in normalized or 'año nuevo' in normalized):
        return year - 1
    if month == 1 and 'diciembre' in normalized and 'enero' not in normalized:
        return year + 1
    return year


def location_after(text, position):
    # A schedule paragraph may contain several dates, so stop at the next one.
    next_date = DATE_RE.search(text, position)
    tail = text[position:next_date.start() if next_date else len(text)]
    for pattern, venue, city in LOCATIONS:
        if re.search(pattern, tail, re.IGNORECASE):
            return venue, city
    return None, None


def records_from_block(title, body, url):
    description = clean_text(body) or None
    if not title or not description:
        return []
    records = []
    text = description
    matches = list(DATE_RE.finditer(text))
    for index, match in enumerate(matches):
        month = MONTHS[match.group('month').lower()]
        year = (
            int(match.group('explicit_year'))
            if match.group('explicit_year')
            else event_year(title, body, month)
        )
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        venue, city = location_after(text[:end], match.end())
        if not venue or not city or not year:
            continue
        raw_days = re.findall(r'\d{1,2}', match.group('days'))
        event_time = match.group('time')
        if event_time:
            hour, minute = (int(part) for part in event_time.replace('.', ':').split(':'))
            event_time = f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None
        for raw_day in raw_days:
            try:
                event_date = date(year, month, int(raw_day)).isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': event_time,
                'venue': venue,
                'city': city,
                'country_code': 'ES',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for slug, splitter in (('proximamente', split_upcoming), ('historico', split_archive)):
        try:
            content = page_content(session, slug)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch concert page',
                event='crawler_page_failed',
                level='warning',
                url=f'{SOURCE_URL}{slug}/',
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for title, body, fragment in splitter(content):
            url = f'{SOURCE_URL}{slug}/'
            if fragment:
                url = f'{url}#{fragment}'
            records.extend(records_from_block(title, body, url))

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class SinfonicaCiudadDeZaragozaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonicaciudaddezaragoza_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SinfonicaCiudadDeZaragozaComCrawler().run()


if __name__ == '__main__':
    main()
