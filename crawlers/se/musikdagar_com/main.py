import re
import zlib
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musikdagar.com/'
SOURCE = 'Östergötlands Musikdagar'
PROGRAM_URL = urljoin(SOURCE_URL, 'home/konsertprogrammet-page')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1, 'februari': 2, 'mars': 3, 'april': 4, 'maj': 5, 'juni': 6,
    'juli': 7, 'augusti': 8, 'september': 9, 'oktober': 10, 'november': 11,
    'december': 12,
}


def _pdf_objects(data):
    return {
        int(match.group(1)): match.group(2)
        for match in re.finditer(rb'(?m)^(\d+)\s+\d+\s+obj\b(.*?)\bendobj\b', data, re.S)
    }


def _stream(obj):
    match = re.search(rb'\bstream\r?\n(.*?)\r?\nendstream\b', obj, re.S)
    if not match:
        return b''
    value = match.group(1)
    if b'/FlateDecode' in obj[:match.start()]:
        value = zlib.decompress(value)
    return value


def _font_maps(objects):
    maps = {}
    for number, obj in objects.items():
        unicode_ref = re.search(rb'/ToUnicode\s+(\d+)\s+\d+\s+R', obj)
        if not unicode_ref:
            continue
        cmap = _stream(objects.get(int(unicode_ref.group(1)), b''))
        mapping = {}
        for start, end, target in re.findall(
            rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', cmap
        ):
            first, last, destination = int(start, 16), int(end, 16), int(target, 16)
            for offset, code in enumerate(range(first, last + 1)):
                mapping[code] = chr(destination + offset)
        maps[number] = mapping
    return maps


def _unescape_pdf_string(value):
    output = bytearray()
    index = 0
    escapes = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8, ord('f'): 12}
    while index < len(value):
        byte = value[index]
        if byte != 92:
            output.append(byte)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        byte = value[index]
        if 48 <= byte <= 55:
            match = re.match(rb'[0-7]{1,3}', value[index:])
            output.append(int(match.group(), 8) & 255)
            index += len(match.group())
        else:
            output.append(escapes.get(byte, byte))
            index += 1
    return bytes(output)


def extract_pdf_lines(data):
    objects = _pdf_objects(data)
    font_maps = _font_maps(objects)
    pages = []
    for _, page in sorted(objects.items()):
        if not re.search(rb'/Type\s*/Page\b', page):
            continue
        content_ref = re.search(rb'/Contents\s+(\d+)\s+\d+\s+R', page)
        if not content_ref:
            continue
        resource_ref = re.search(rb'/Resources\s+(\d+)\s+\d+\s+R', page)
        resources = objects.get(int(resource_ref.group(1)), b'') if resource_ref else page
        fonts = {
            name.decode(): font_maps.get(int(reference), {})
            for name, reference in re.findall(rb'/(TT\d+)\s+(\d+)\s+\d+\s+R', resources)
        }
        content = _stream(objects.get(int(content_ref.group(1)), b''))
        spans = []
        for block in re.findall(rb'BT\b(.*?)\bET', content, re.S):
            font_match = re.search(rb'/(TT\d+)\s+[\d.]+\s+Tf', block)
            position = re.search(
                rb'[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\s+([-\d.]+)\s+Tm',
                block,
            )
            if not font_match or not position:
                continue
            mapping = fonts.get(font_match.group(1).decode(), {})
            chunks = re.findall(rb'\((?:\\.|[^\\)])*\)', block)
            text = ''.join(
                ''.join(mapping.get(byte, '') for byte in _unescape_pdf_string(chunk[1:-1]))
                for chunk in chunks
            ).strip()
            if text:
                spans.append((float(position.group(2)), float(position.group(1)), text))
        rows = []
        for y, x, text in sorted(spans, key=lambda item: (-item[0], item[1])):
            if not rows or abs(rows[-1][0] - y) > 3:
                rows.append([y, [(x, text)]])
            else:
                rows[-1][1].append((x, text))
        pages.append([' '.join(text for _, text in sorted(parts)).strip() for _, parts in rows])
    return pages


def _parse_date(line, year):
    match = re.search(r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\b', line.lower())
    if not match:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


KNOWN_CITIES = ('Linköping', 'Norrköping', 'Åtvidaberg', 'Vadstena', 'Kinda', 'Bjärka-Säby')
VENUE_DEFAULTS = {
    'Norrköpings Konstmuseum': 'Norrköping',
    'Vadstena Klosterkyrka': 'Vadstena',
    'Gästabudssalen, Löfstad slott': 'Norrköping',
}


def _normalize_pdf_text(value):
    value = re.sub(r'(?<=\d)\s+(?=\d)', '', value)
    value = re.sub(r'(?<=\w)\s+-\s+(?=\w)', '-', value)
    value = re.sub(r'\bKind\s+a\b', 'Kinda', value)
    return re.sub(r'\s+', ' ', value).strip()


def _parse_location(value):
    value = _normalize_pdf_text(value).strip(' –-')
    for venue, city in sorted(VENUE_DEFAULTS.items(), key=lambda item: -len(item[0])):
        if value.startswith(venue):
            return venue, city, value[len(venue):].strip(' –-')
    for city in KNOWN_CITIES:
        match = re.search(rf',\s*{re.escape(city)}\b', value)
        if match:
            venue = value[:match.start()].strip(' ,')
            remainder = value[match.end():].strip(' –-')
            return venue, city, remainder
    return None


def parse_program(pages, pdf_url):
    lines = [_normalize_pdf_text(line) for page in pages for line in page]
    text = '\n'.join(lines)
    year_match = re.search(r'\b(20\d{2})\b', text)
    if not year_match:
        raise ValueError('Could not determine program year')
    year = int(year_match.group(1))
    records = []
    current_date = None
    occurrences = []
    for line in lines:
        heading_date = _parse_date(line, year) if re.match(
            r'(?i)^(?:måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag)\b', line
        ) else None
        if heading_date:
            current_date = heading_date
            continue
        occurrence = re.match(r'^-\s*([01]?\d|2[0-3])[.:]([0-5]\d)\s+(.+)$', line)
        if occurrence and current_date:
            occurrences.append({
                'date': current_date,
                'time': f'{int(occurrence.group(1)):02d}:{occurrence.group(2)}',
                'lines': [occurrence.group(3)],
            })
            continue
        if occurrences and not re.match(r'(?i)^FRI ENTR|^Biljetter från|^Kvarvarande biljetter', line):
            occurrences[-1]['lines'].append(line)

    for occurrence in occurrences:
        body = occurrence['lines']
        location = _parse_location(body[0])
        if not location:
            continue
        venue, city, title = location
        description_lines = body[1:]
        if not title and description_lines:
            title = description_lines[0]
        if not venue or not city or not title:
            continue
        title = re.split(r'\s+Schuberts\b', title, maxsplit=1)[0].strip(' .–-')
        description = '\n'.join(([title] if title else []) + description_lines) or None
        records.append({
            'title': title,
            'date': occurrence['date'],
            'url': pdf_url,
            'time_from': occurrence['time'],
            'venue': venue,
            'city': city,
            'country_code': 'SE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class MusikdagarComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikdagar_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(PROGRAM_URL, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            pdf_link = soup.select_one('a[href$=".pdf"], a[href*=".pdf?"]')
            if pdf_link is None:
                raise ValueError('Could not find the first-party program PDF')
            pdf_url = urljoin(PROGRAM_URL, pdf_link['href'])
            pdf_response = session.get(pdf_url, timeout=45)
            pdf_response.raise_for_status()
            if not pdf_response.content.startswith(b'%PDF-'):
                raise ValueError('Program download is not a PDF')
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Musikdagar program', event='crawler_fetch_failed', level='error',
                url=PROGRAM_URL, error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = parse_program(extract_pdf_lines(pdf_response.content), pdf_url)
        if not records:
            raise ValueError('No concert occurrences could be parsed from the program PDF')
        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    MusikdagarComCrawler().run()


if __name__ == '__main__':
    main()
