import html
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://karin-rehnqvist.se/'
SOURCE = 'Karin Rehnqvist'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/posts')
CALENDAR_CATEGORY_IDS = (25, 34)  # "Kalendarium" and "Kalendarier"
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1, 'january': 1,
    'februari': 2, 'february': 2,
    'mars': 3, 'march': 3,
    'april': 4,
    'maj': 5, 'may': 5,
    'juni': 6, 'june': 6,
    'juli': 7, 'july': 7,
    'augusti': 8, 'august': 8,
    'september': 9,
    'oktober': 10, 'october': 10,
    'november': 11,
    'december': 12,
}
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))
DATE_RE = re.compile(
    rf'(?P<days>\d{{1,2}}(?:\s*(?:och|and|&|,|\+|[–—-])\s*\d{{1,2}})*)'
    rf'\s+(?P<month>{MONTH_PATTERN})(?:\s+(?P<year>20\d{{2}}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(?:kl\.?\s*)?(?P<hour>[01]?\d|2[0-3])[.:](?P<minute>[0-5]\d)\b'
    r'|\b(?P<hour12>1[0-2]|0?[1-9])(?:[.:](?P<minute12>[0-5]\d))?\s*(?P<ampm>[ap])\.?m\.\b',
    re.IGNORECASE,
)

# Calendar entries are international and usually end in "venue, city".  The
# site does not expose structured addresses, so only explicitly recognised
# cities are accepted; uncertain entries are deliberately skipped.
CITY_COUNTRIES = {
    'Amsterdam': 'NL', 'Aten': 'GR', 'Athens': 'GR', 'Barcelona': 'ES',
    'Berlin': 'DE', 'Bergen': 'NO', 'Birmingham': 'GB', 'Bonn': 'DE',
    'Boston': 'US', 'Bratislava': 'SK', 'Brussels': 'BE', 'Bryssel': 'BE',
    'Budapest': 'HU', 'Chicago': 'US', 'Dublin': 'IE', 'Edinburgh': 'GB',
    'Espoo': 'FI', 'Florens': 'IT', 'Frankfurt': 'DE', 'Graz': 'AT',
    'Hamburg': 'DE', 'Helsingfors': 'FI', 'Helsinki': 'FI', 'Istanbul': 'TR',
    'Köpenhamn': 'DK', 'Copenhagen': 'DK', 'Leipzig': 'DE', 'London': 'GB',
    'Los Angeles': 'US', 'Malmö': 'SE', 'München': 'DE', 'Munich': 'DE',
    'New York': 'US', 'Oslo': 'NO', 'Paris': 'FR', 'Parchim': 'DE',
    'Prag': 'CZ', 'Prague': 'CZ', 'Reykjavik': 'IS', 'Riga': 'LV',
    'Rom': 'IT', 'Rome': 'IT', 'Salzburg': 'AT', 'Tallinn': 'EE',
    'Toronto': 'CA', 'Turin': 'IT', 'Torino': 'IT', 'Utrecht': 'NL',
    'Wien': 'AT', 'Vienna': 'AT', 'Zagreb': 'HR', 'Zürich': 'CH',
    'Alingsås': 'SE', 'Berg': 'SE', 'Borlänge': 'SE', 'Borås': 'SE',
    'Enköping': 'SE', 'Eskilstuna': 'SE', 'Falkenberg': 'SE',
    'Falköping': 'SE', 'Gävle': 'SE', 'Göteborg': 'SE', 'Halmstad': 'SE',
    'Hässleholm': 'SE', 'Jönköping': 'SE', 'Kalmar': 'SE', 'Karlskrona': 'SE',
    'Karlstad': 'SE', 'Kristianstad': 'SE', 'Lammhult': 'SE', 'Linköping': 'SE',
    'Luleå': 'SE', 'Lund': 'SE', 'Norrköping': 'SE', 'Osby': 'SE',
    'Skövde': 'SE', 'Stockholm': 'SE', 'Stallarholmen': 'SE',
    'Sundbyberg': 'SE', 'Sundsvall': 'SE', 'Umeå': 'SE', 'Uppsala': 'SE',
    'Västerås': 'SE', 'Växjö': 'SE', 'Visby': 'SE', 'Örebro': 'SE',
    'Östersund': 'SE', 'Ribe': 'DK', 'Valldemosa': 'ES', 'Mallorca': 'ES',
    'North Yorkshire': 'GB', 'Ochsenhausen': 'DE',
    'Bodø': 'NO', 'Tromsö': 'NO', 'Tromsø': 'NO', 'Longyearbyen': 'NO',
    'Mora': 'SE', 'Orsa': 'SE', 'Älvdalen': 'SE', 'Solna': 'SE',
    'Mariefred': 'SE', 'Nacka': 'SE', 'Vadstena': 'SE', 'Lodz': 'PL',
    'San Francisco': 'US', 'Berkeley': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_calendar_posts(session):
    page = 1
    posts = []
    while True:
        response = session.get(
            API_URL,
            params={
                'categories': ','.join(map(str, CALENDAR_CATEGORY_IDS)),
                'per_page': 10,
                'page': page,
                'orderby': 'date',
                'order': 'desc',
                '_fields': 'id,link,title,content,categories',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return posts
        page += 1


def content_blocks(content):
    soup = BeautifulSoup(content, 'html.parser')
    # The older archive is stored as one table row per occurrence. Newer posts
    # use either one paragraph per occurrence or several paragraphs separated
    # by horizontal rules. Grouping on both dates and rules handles both eras.
    table_cells = soup.select('td')
    if table_cells:
        for node in table_cells:
            for br in node.select('br'):
                br.replace_with('\n')
            text = clean_text(node.get_text('\n', strip=True))
            if text:
                yield text
        return

    grouped = []
    for node in soup.children:
        name = getattr(node, 'name', None)
        if name == 'hr':
            if grouped:
                yield clean_text('\n'.join(grouped))
                grouped = []
            continue
        if name not in {'p', 'li'}:
            continue
        for br in node.select('br'):
            br.replace_with('\n')
        text = clean_text(node.get_text('\n', strip=True))
        if not text:
            continue
        if DATE_RE.search(text[:100]) and grouped:
            yield clean_text('\n'.join(grouped))
            grouped = []
        grouped.append(text)
    if grouped:
        yield clean_text('\n'.join(grouped))


def block_dates(text, fallback_year):
    # A block is an occurrence only when it starts with a calendar date.  This
    # excludes surrounding prose and undated school-tour notices.
    match = DATE_RE.search(text[:100])
    if not match or text[:match.start()].strip(' –-—'):
        return []
    year = int(match.group('year') or fallback_year)
    month = MONTHS[match.group('month').casefold()]
    days = [int(value) for value in re.findall(r'\d{1,2}', match.group('days'))]
    values = []
    for day in days:
        try:
            values.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return values


def block_title(lines):
    if not lines:
        return ''
    first = DATE_RE.sub('', lines[0], count=1).strip(' –-—:')
    if first and not re.fullmatch(r'(uruppförande|premiär)', first, re.IGNORECASE):
        return first
    if len(lines) > 1:
        return lines[1].strip(' –-—:')
    return ''


def block_times(text):
    matches = list(TIME_RE.finditer(text))
    # In "kl 13.00–14.30" the latter value is an end time, not a second
    # occurrence. Conversely "kl 13 och kl 15" describes two performances.
    if len(matches) >= 2:
        between = text[matches[0].end():matches[1].start()]
        if re.fullmatch(r'\s*[–—-]\s*', between):
            matches = matches[:1]
    values = []
    for match in matches:
        if match.group('hour') is not None:
            hour = int(match.group('hour'))
            minute = int(match.group('minute'))
        else:
            hour = int(match.group('hour12')) % 12
            if match.group('ampm').casefold() == 'p':
                hour += 12
            minute = int(match.group('minute12') or 0)
        value = f'{hour:02d}:{minute:02d}'
        if value not in values:
            values.append(value)
    return values or [None]


def block_location(lines):
    candidates = []
    for line in lines[1:]:
        folded = line.casefold()
        for city, code in CITY_COUNTRIES.items():
            match = re.search(rf'(?<!\w){re.escape(city)}(?!\w)', line, re.IGNORECASE)
            if match:
                candidates.append((line, match, city, code))
    if not candidates:
        return None

    line, match, city, code = candidates[-1]
    before = line[:match.start()].rstrip(' ,;–-—')
    # Location lines sometimes begin with a festival name. Keeping it together
    # with the hall is preferable to guessing which comma-delimited phrase is
    # the physical venue.
    venue = re.sub(r'^(?:Earth Hour|Festivalen?\s+[^,]+),\s*', '', before, flags=re.IGNORECASE)
    venue = venue.strip(' ,;–-—')
    if not venue:
        return None
    return venue, city, code


def records_from_post(post):
    title_text = clean_text(post.get('title', {}).get('rendered'))
    year_match = re.search(r'20\d{2}', title_text)
    if not year_match:
        return []
    fallback_year = int(year_match.group())
    post_url = post.get('link')
    records = []
    for block in content_blocks(post.get('content', {}).get('rendered', '')):
        dates = block_dates(block, fallback_year)
        if not dates:
            continue
        lines = [line for line in block.splitlines() if clean_text(line)]
        title = clean_text(block_title(lines))
        location = block_location(lines)
        if not title or not location:
            continue
        venue, city, country_code = location
        for event_date in dates:
            for event_time in block_times(block):
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': post_url,
                    'time_from': event_time,
                    'venue': venue,
                    'city': city,
                    'country_code': country_code,
                    'description': block,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
    return records


class KarinRehnqvistSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='karin_rehnqvist_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        posts = fetch_calendar_posts(session)
        records = []
        for post in posts:
            try:
                records.extend(records_from_post(post))
            except (AttributeError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Karin Rehnqvist calendar post',
                    event='crawler_item_failed',
                    level='warning',
                    url=post.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    KarinRehnqvistSeCrawler().run()


if __name__ == '__main__':
    main()
