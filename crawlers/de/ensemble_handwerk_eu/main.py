import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ensemble-handwerk.eu/'
SOURCE = 'hand werk'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
EVENT_CATEGORIES = '15,20'  # Termine and Veranstaltungsarchiv
HEADERS = {'User-Agent': 'classical-concert-crawler/1.0'}

MONTHS = {
    'januar': 1, 'jan': 1, 'februar': 2, 'feb': 2, 'märz': 3, 'maerz': 3,
    'mar': 3, 'april': 4, 'apr': 4, 'mai': 5, 'may': 5, 'juni': 6,
    'jun': 6, 'juli': 7, 'jul': 7, 'august': 8, 'aug': 8,
    'september': 9, 'sept': 9, 'sep': 9, 'oktober': 10, 'okt': 10,
    'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
    'dezember': 12, 'dez': 12, 'december': 12, 'dec': 12,
}
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))
DATE_RE = re.compile(
    rf'(?<!\d)(?P<day>\d{{1,2}})\s*[._-]?\s*'
    rf'(?P<month>{MONTH_PATTERN})\s*[._-]?\s*(?P<year>20\d{{2}})', re.I,
)
NUMERIC_DATE_RE = re.compile(r'(?<!\d)(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./](?P<year>20\d{2})')
TIME_RE = re.compile(
    r'(?<!\d)(\d{1,2})(?:[.:](\d{2}))?'
    r'(?:\s*[-–]\s*\d{1,2}(?:[.:]\d{2})?)?\s*(?:Uhr|h)(?![A-Za-z])'
    r'|(?<!\d)(\d{1,2})[.:](\d{2})(?!\d)',
    re.I,
)

# The archive contains tours as well as Cologne concerts. These aliases are
# deliberately city-specific; no home-city fallback is applied to tour posts.
CITY_DATA = {
    'Aachen': ('DE', ('aachen',)), 'Aveiro': ('PT', ('aveiro', 'aviero')),
    'Berlin': ('DE', ('berlin',)), 'Chios': ('GR', ('chios',)),
    'Cologne': ('DE', ('köln', 'koeln', 'cologne')), 'Daegu': ('KR', ('daegu',)),
    'Düsseldorf': ('DE', ('düsseldorf', 'duesseldorf')), 'Essen': ('DE', ('essen',)),
    'Freiburg': ('DE', ('freiburg',)), 'Göttingen': ('DE', ('göttingen', 'goettingen')),
    'Graz': ('AT', ('graz',)), 'Hamburg': ('DE', ('hamburg',)),
    'Hannover': ('DE', ('hannover',)), 'Heidenheim': ('DE', ('heidenheim',)),
    'Izmir': ('TR', ('izmir', 'i̇zmir')), 'Jeju': ('KR', ('jeju',)),
    'La Paz': ('BO', ('la paz',)), 'Ludwigshafen': ('DE', ('ludwigshafen',)),
    'Munich': ('DE', ('münchen', 'munich')), 'Münster': ('DE', ('münster', 'muenster')),
    'Rheinsberg': ('DE', ('rheinsberg',)), 'Straubing': ('DE', ('straubing',)),
    'Tokyo': ('JP', ('tokyo',)), 'Utrecht': ('NL', ('utrecht',)),
    'Warsaw': ('PL', ('warschau', 'warsaw')), 'Wiesbaden': ('DE', ('wiesbaden',)),
    'Witten': ('DE', ('witten',)), 'Weingarten': ('DE', ('weingarten',)),
}
VENUE_ALIASES = {
    'alte feuerwache': 'Alte Feuerwache', 'ms stubnitz': 'MS Stubnitz',
    'walkmühle': 'Walkmühle', 'neue aula': 'Neue Aula, Folkwang Universität der Künste',
    'folkwang universität': 'Folkwang Universität der Künste',
    'museum kolumba': 'Museum Kolumba', 'klangbrücke': 'Klangbrücke Aachen',
    'musikhochschule münster': 'Musikhochschule Münster',
    'ahmed adnan saygun': 'Ahmed Adnan Saygun Cultural Center',
    'christuskirche': 'Christuskirche', 'kölner philharmonie': 'Kölner Philharmonie',
    'musa probebühne': 'musa Probebühne', 'kunstquartier bethanien': 'Kunstquartier Bethanien',
    'basf-gesellschaftshaus': 'BASF-Gesellschaftshaus', 'saalbau witten': 'Saalbau Witten',
    'sprengel museum': 'Sprengel Museum', 'stadtbibliothek heidenheim': 'Stadtbibliothek Heidenheim',
    'forum stadtpark': 'Forum Stadtpark', 'tivoli vredenburg': 'TivoliVredenburg',
    'daegu concerthall': 'Daegu Concert House', 'spyros stefanou': 'Spyros Stefanou Estate',
    'anton bruckner gymnasium': 'Anton-Bruckner-Gymnasium',
    'hochschule für musik freiburg': 'Hochschule für Musik Freiburg',
    'hfmt köln': 'Hochschule für Musik und Tanz Köln', 'partika-saal': 'Partika-Saal',
    'rittersaal': 'Rittersaal im Herzogschloss', 'quincy passage': 'Quincy Passage',
    'museum ludwig': 'Museum Ludwig', 'raw-gelände': 'RAW-Gelände',
    'pumpenhaus': 'Theater im Pumpenhaus', 'bürgermeisterhaus': 'Bürgermeisterhaus',
    'hotel am chlodwigplatz': 'Hotel am Chlodwigplatz', 'freiheizhalle': 'Freiheizhalle',
    'hochschule für musik und theater hamburg': 'Hochschule für Musik und Theater Hamburg',
    'werk°stadt witten': 'WERK°STADT Witten',
    'pädagogische hochschule': 'Pädagogische Hochschule Weingarten',
}
VENUE_LOCATIONS = {
    'Alte Feuerwache': ('Cologne', 'DE'), 'WERK°STADT Witten': ('Witten', 'DE'),
    'Pädagogische Hochschule Weingarten': ('Weingarten', 'DE'),
    'Ahmed Adnan Saygun Cultural Center': ('Izmir', 'TR'),
    'Musikhochschule Münster': ('Münster', 'DE'),
    'Kölner Philharmonie': ('Cologne', 'DE'), 'Museum Kolumba': ('Cologne', 'DE'),
    'Klangbrücke Aachen': ('Aachen', 'DE'), 'MS Stubnitz': ('Hamburg', 'DE'),
    'Walkmühle': ('Wiesbaden', 'DE'), 'musa Probebühne': ('Göttingen', 'DE'),
    'Sprengel Museum': ('Hannover', 'DE'), 'Saalbau Witten': ('Witten', 'DE'),
    'Stadtbibliothek Heidenheim': ('Heidenheim', 'DE'),
    'Folkwang Universität der Künste': ('Essen', 'DE'),
    'Theater im Pumpenhaus': ('Münster', 'DE'), 'Bürgermeisterhaus': ('Essen', 'DE'),
    'Hotel am Chlodwigplatz': ('Cologne', 'DE'),
    'Hochschule für Musik Freiburg': ('Freiburg', 'DE'),
    'Hochschule für Musik und Tanz Köln': ('Cologne', 'DE'),
}


def clean_text(value):
    text = BeautifulSoup(html.unescape(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_dates(text):
    found = []
    for regex in (DATE_RE, NUMERIC_DATE_RE):
        for match in regex.finditer(text):
            month_raw = match.group('month')
            month = int(month_raw) if month_raw.isdigit() else MONTHS[month_raw.casefold()]
            try:
                value = date(int(match.group('year')), month, int(match.group('day'))).isoformat()
            except ValueError:
                continue
            found.append((match.start(), match.end(), value))
    return sorted(set(found), key=lambda item: item[0])


def city_for(text):
    folded = text.casefold()
    matches = []
    for city, (country, aliases) in CITY_DATA.items():
        positions = [folded.find(alias) for alias in aliases if folded.find(alias) >= 0]
        if positions:
            matches.append((min(positions), city, country))
    return min(matches)[1:] if matches else (None, None)


def venue_for(text, anchor=0):
    folded = text.casefold()
    matches = []
    for alias, venue in VENUE_ALIASES.items():
        for match in re.finditer(re.escape(alias), folded):
            # A location following the date belongs to it more often than a
            # location preceding it (important in multi-date tour posts).
            distance = abs(match.start() - anchor) + (100 if match.start() < anchor else 0)
            matches.append((distance, venue))
    return min(matches)[1] if matches else None


def parse_post(post):
    title = clean_text((post.get('title') or {}).get('rendered'))
    description = clean_text((post.get('content') or {}).get('rendered'))
    url = post.get('link') or ''
    if not title or not description or not url:
        return []

    # Programme prose sometimes repeats a date from the schedule. Keep the
    # first occurrence; it is the one adjacent to the event's location.
    dates = []
    seen_dates = set()
    for occurrence in event_dates(description):
        if occurrence[0] > 600:
            continue
        if occurrence[2] not in seen_dates:
            dates.append(occurrence)
            seen_dates.add(occurrence[2])
    records = []
    for index, (start, end, event_date) in enumerate(dates):
        left = max(0, start - 120)
        right = dates[index + 1][0] if index + 1 < len(dates) else min(len(description), end + 350)
        context = description[left:right]
        city, country_code = city_for(context)
        venue = venue_for(context, start - left)
        if not city or not venue:
            # Some lines name the date before the venue and city; widen locally,
            # but never use the ensemble's Cologne home as a tour fallback.
            wide_left = max(0, start - 250)
            wide_right = min(len(description), end + 700)
            if index + 1 < len(dates) and dates[index + 1][2][:7] != event_date[:7]:
                wide_right = dates[index + 1][0]
            context = description[wide_left:wide_right]
            city, country_code = city_for(context)
            venue = venue_for(context, start - wide_left)
        if venue in VENUE_LOCATIONS:
            city, country_code = VENUE_LOCATIONS[venue]
        time_match = TIME_RE.search(description[end:min(len(description), end + 35)])
        time_from = None
        if time_match:
            hour = int(time_match.group(1) or time_match.group(3))
            minute = int(time_match.group(2) or time_match.group(4) or 0)
            if hour < 24 and minute < 60:
                time_from = f'{hour:02d}:{minute:02d}'
        if not city or not venue:
            log_message(
                'Skipped hand werk date without defensible venue or city',
                event='crawler_item_skipped', level='warning', url=url,
                error_type='IncompleteEventData',
                error_message=f'Missing venue or city for {event_date}',
            )
            continue
        records.append({
            'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
            'venue': venue, 'city': city, 'country_code': country_code,
            'description': description, 'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return records


class EnsembleHandwerkEuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemble_handwerk_eu', source=SOURCE, source_url=SOURCE_URL,
        country_code='DE', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            API_URL,
            params={
                'categories': EVENT_CATEGORIES, 'per_page': 100,
                '_fields': 'id,link,title,content',
            },
            headers=HEADERS, timeout=45,
        )
        response.raise_for_status()
        records = []
        for post in response.json():
            records.extend(parse_post(post))
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    EnsembleHandwerkEuCrawler().run()


if __name__ == '__main__':
    main()
