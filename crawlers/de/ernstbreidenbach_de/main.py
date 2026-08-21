import re
from datetime import date

import requests
from bs4 import BeautifulSoup, Comment

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ernstbreidenbach.de/'
CALENDAR_URL = f'{SOURCE_URL}daten/konzerte.htm'
SOURCE = 'Ernst Breidenbach'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'dezember': 12,
}

# The archive also contains recordings, releases, and broadcasts. These are not
# public performances under the project's inclusion guidance.
NON_EVENTS = re.compile(
    r'CD[- ](?:Produktion|Veröffentlichung)|Veröffentlichung der (?:neuen )?CD|'
    r'Podcast|Radiosendung|Sendung mit Ausschnitten|Musik-Panorama|'
    r'Produktion (?:Hessischer|Julius|Reinhold|Toni)|Deutschlandfunk\s*/\s*cpo|'
    r'1\.\s*[–-]\s*24\.\s*Juni\s*2016',
    re.I,
)

# Location wording varies throughout this hand-maintained archive. Each rule is
# deliberately tied to wording on the page, rather than guessing from the artist's
# home town. More specific rules must precede general ones.
LOCATION_RULES = [
    (r'Matthäuskirche Stuttgart-Hasloch', 'Stuttgart', 'Matthäuskirche Stuttgart-Heslach', 'DE'),
    (r'Haus Wahnfried', 'Bayreuth', 'Haus Wahnfried', 'DE'),
    (r'Köln Gürzenich', 'Köln', 'Gürzenich', 'DE'),
    (r'Heidelberg-Neuenheim, Johanneskirche', 'Heidelberg', 'Johanneskirche', 'DE'),
    (r'Bensheim, Katholische Stadtkirche St\. Georg', 'Bensheim', 'Katholische Stadtkirche St. Georg', 'DE'),
    (r'Friedenskirche Ludwigshafen', 'Ludwigshafen', 'Friedenskirche', 'DE'),
    (r'Hochschule für Kirchenmusik, Heidelberg', 'Heidelberg', 'Hochschule für Kirchenmusik', 'DE'),
    (r'Johanneskirche Berlin-Frohnau', 'Berlin', 'Johanneskirche Frohnau', 'DE'),
    (r'Darmstadt\s*-\s*Akademie für Tonkunst|Darmstadt, Akademie für Tonkunst|Akademie für Tonkunst,?\s*Darmstadt', 'Darmstadt', 'Akademie für Tonkunst', 'DE'),
    (r'Hochschule für Musik Saar, Saarbrücken', 'Saarbrücken', 'Hochschule für Musik Saar', 'DE'),
    (r'Friedenskirche.*68165 Mannheim', 'Mannheim', 'Friedenskirche', 'DE'),
    (r'Akademie für Tonkunst,\s*Ludwighöhstr', 'Darmstadt', 'Akademie für Tonkunst', 'DE'),
    (r'Heiliggeistkirche Bern|Programm 1[67]\.6\.2016', 'Bern', 'Heiliggeistkirche', 'CH'),
    (r'Zipfen, Hofreite', 'Zipfen', 'Hofreite', 'DE'),
    (r'Gelnhausen, Romanisches Haus', 'Gelnhausen', 'Romanisches Haus', 'DE'),
    (r'Leipzig, HfMT|Musikhochschule Leipzig', 'Leipzig', 'HfMT Felix Mendelssohn Bartholdy', 'DE'),
    (r'Mannheim, Christuskirche', 'Mannheim', 'Christuskirche', 'DE'),
    (r'Berlin, Musikinstrumentenmuseum', 'Berlin', 'Musikinstrumentenmuseum', 'DE'),
    (r'Akademie für Tonkunst Darmstadt', 'Darmstadt', 'Akademie für Tonkunst', 'DE'),
    (r'Reichelsheim, St\. Michaelis', 'Reichelsheim', 'Michaelskirche', 'DE'),
    (r'Ossiach, Stiftskirche', 'Ossiach', 'Stiftskirche Ossiach', 'AT'),
    (r'Akademie für Tonkunst, Darmstadt', 'Darmstadt', 'Akademie für Tonkunst', 'DE'),
    (r'Duoabend Dieburg, Fechenbacher Schloss', 'Dieburg', 'Fechenbacher Schloss', 'DE'),
]


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{2,}', '\n', text).strip()


def split_sections(html):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('.text')
    if not container:
        return []
    for comment in container.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for element in container.select('img, video, script, style'):
        element.decompose()

    sections = []
    current = []
    for node in container.children:
        if getattr(node, 'get', lambda *_: None)('class') == ['hr']:
            text = clean_text(''.join(current))
            if text:
                sections.append(text)
            current = []
            continue
        if getattr(node, 'name', None) == 'br':
            current.append('\n')
        elif getattr(node, 'get_text', None):
            current.append(node.get_text(' ', strip=True))
        else:
            current.append(str(node))
    tail = clean_text(''.join(current))
    if tail:
        sections.append(tail)
    return sections


def parse_date(text):
    numeric = re.search(r'(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})', text)
    if numeric:
        day, month, year = map(int, numeric.groups())
    else:
        named = re.search(
            r'(?<!\d)(\d{1,2})\.\s*'
            r'(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)'
            r'\s+(20\d{2})',
            text,
            re.I,
        )
        if not named:
            return None
        day, year = int(named.group(1)), int(named.group(3))
        month = MONTHS[named.group(2).lower()]
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_time(text):
    time_range = re.search(
        r'(?<!\d)(\d{1,2}):(\d{2})\s*[–-]\s*\d{1,2}:\d{2}\s*Uhr',
        text,
        re.I,
    )
    if time_range:
        return f'{int(time_range.group(1)):02d}:{time_range.group(2)}'
    match = re.search(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*Uhr', text, re.I)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_location(text):
    for pattern, city, venue, country_code in LOCATION_RULES:
        if re.search(pattern, text, re.I | re.S):
            return city, venue, country_code
    return None, None, None


def event_title(text):
    lines = [line.strip(' ,') for line in text.splitlines() if line.strip()]
    priorities = (
        'Überall Wahnfried', 'VOKAL', 'SOMMERKONZERT', 'Petite Messe',
        'Duo Pianarmonio', 'Duoabend', 'Klavierabend', 'Klavierrecital',
        'Liederabend', 'Hauskonzert', 'Orgelfestival', 'Werke für Violine',
        'Werke von',
    )
    for phrase in priorities:
        for line in lines:
            if phrase.casefold() in line.casefold():
                return line[:250]
    for line in lines:
        if not re.search(r'20\d{2}|Uhr|straße|str\.|Kirche|Hochschule|Akademie', line, re.I):
            return line[:250]
    return None


def parse_section(text, index):
    if NON_EVENTS.search(text):
        return None
    event_date = parse_date(text)
    city, venue, country_code = parse_location(text)
    title = event_title(text)
    if not event_date or not city or not venue or not title:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': f'{CALENDAR_URL}#event-{event_date}-{index}',
        'time_from': parse_time(text),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': text,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ErnstbreidenbachDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ernstbreidenbach_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = []
        sections = split_sections(response.content)
        for index, section in enumerate(sections):
            record = parse_section(section, index)
            if record:
                records.append(record)
        log_message(
            'Parsed Ernst Breidenbach concert archive',
            event='crawler_scrape_parsed',
            url=CALENDAR_URL,
            section_count=len(sections),
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ErnstbreidenbachDeCrawler().run()


if __name__ == '__main__':
    main()
