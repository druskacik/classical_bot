import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cantusbasel.ch/'
SOURCE = 'Cantus Basel'
FUTURE_URL = urljoin(SOURCE_URL, 'konzerte/zukunftsmusik')
ARCHIVE_URL = urljoin(SOURCE_URL, 'konzerte/rueckblick')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'maerz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_RE = re.compile(
    rf'(?P<day>\d{{1,2}})\.\s*(?P<month>{MONTH_PATTERN})\s+(?P<year>\d{{4}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s*Uhr', re.IGNORECASE)
WEEKDAY_RE = re.compile(
    r'\b(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)\b,?', re.IGNORECASE
)

CITY_MARKERS = [
    ('Böblingen', 'DE'),
    ('Zürich', 'CH'),
    ('Solothurn', 'CH'),
    ('Liestal', 'CH'),
    ('Arlesheim', 'CH'),
    ('Riehen', 'CH'),
    ('Basel', 'CH'),
]


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(match):
    month = MONTHS[match.group('month').lower()]
    try:
        return date(int(match.group('year')), month, int(match.group('day'))).isoformat()
    except ValueError:
        return None


def parse_time(text, date_match):
    match = TIME_RE.search(text, date_match.end())
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def parse_location(text, date_match):
    before = text[:date_match.start()]
    after = text[date_match.end():]
    after = TIME_RE.sub('', after, count=1)
    before = WEEKDAY_RE.sub('', before)
    after = WEEKDAY_RE.sub('', after)

    # The site consistently writes either "venue, date" or "date, venue".
    before = re.sub(r'^[\s,:;*]+|[\s,:;*]+$', '', before)
    after = re.sub(r'^[\s,:;*]+|[\s,:;*]+$', '', after)
    venue = after if after else before
    venue = re.split(r'\s*/\s*(?:Verschoben|Schliesslich)', venue, maxsplit=1)[0].strip()
    venue = re.sub(r'^(?:Ort\s*:\s*)', '', venue, flags=re.IGNORECASE)

    combined = f'{venue} {text}'
    for city, country_code in CITY_MARKERS:
        if re.search(rf'\b{re.escape(city)}\b', combined, re.IGNORECASE):
            if venue and venue.casefold() != city.casefold():
                return venue, city, country_code
    return None


def title_for_block(block, lines, date_line):
    heading = block.select_one('h2.element-header, h2, h3.element-header')
    if heading and clean_text(heading):
        return clean_text(heading)

    for element in block.select('strong'):
        candidate = clean_text(element).strip(' :')
        if candidate and not DATE_RE.search(candidate) and len(candidate) >= 3:
            return candidate

    for index, line in enumerate(lines):
        candidate = line.strip(' :')
        if (
            candidate != date_line
            and not DATE_RE.search(candidate)
            and not re.match(r'^Ort\s*:', line, re.IGNORECASE)
            and len(candidate) >= 3
        ):
            if index + 1 < len(lines) and lines[index + 1].lstrip().startswith(':'):
                candidate = f'{candidate}{lines[index + 1]}'
            return candidate[:250]
    return None


def parse_block(block, page_url):
    text = clean_text(block)
    if not text or re.search(r'\b(?:abgesagt|nicht aufgeführt)\b', text, re.IGNORECASE):
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    description = text or None
    records = []
    for line_index, line in enumerate(lines):
        if re.search(r'Ankündigung|Konzertkritik|Zeitung|Presse', line, re.IGNORECASE):
            continue
        for match in DATE_RE.finditer(line):
            event_date = parse_date(match)
            location_line = line
            # Newer ClubDesk layouts put "Ort: ..." in the next paragraph.
            if line_index + 1 < len(lines) and re.match(
                r'^Ort\s*:', lines[line_index + 1], re.IGNORECASE
            ):
                location_line = f'{line} {lines[line_index + 1]}'
            location = parse_location(location_line, match)
            title = title_for_block(block, lines, line)
            if not event_date or not location or not title:
                continue
            venue, city, country_code = location
            records.append({
                'title': title,
                'date': event_date,
                'url': f'{page_url}#{block.get("id", "concerts")}',
                'time_from': parse_time(location_line, match),
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class CantusbaselChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cantusbasel_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
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
        records = []
        for page_url in (FUTURE_URL, ARCHIVE_URL):
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Cantus Basel concerts',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            blocks = soup.select('div.cd-block-content[id$="_content"]')
            for block_index, block in enumerate(blocks):
                block_records = parse_block(block, page_url)
                if block_records:
                    # Programme notes are separate accordion blocks on recent pages.
                    for extra_block in blocks[block_index + 1:block_index + 4]:
                        extra_text = clean_text(extra_block)
                        if extra_text.startswith('Informationen zu'):
                            for record in block_records:
                                record['description'] = (
                                    f"{record['description']}\n\n{extra_text}"
                                )
                            break
                    records.extend(block_records)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['venue'], record['title']
            ),
        )


def main():
    CantusbaselChCrawler().run()


if __name__ == '__main__':
    main()
