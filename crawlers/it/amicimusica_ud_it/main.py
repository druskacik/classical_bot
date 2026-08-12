import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.amicimusica.ud.it/it/'
CALENDAR_URL = 'https://www.amicimusica.ud.it/it/it/node/60'
TORRIANI_URL = 'https://www.amicimusica.ud.it/it/it/node/25'
CALENDAR_URLS = (CALENDAR_URL, TORRIANI_URL)
SOURCE = 'Amici della Musica Udine'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}
WEEKDAYS = r'(?:lunedi|martedi|mercoledi|giovedi|venerdi|sabato|domenica)'
DATE_RE = re.compile(
    rf'(?i)\b{WEEKDAYS}[\s\u00a0]+(?:'
    rf'(\d{{1,2}})[\s\u00a0]+({"|".join(MONTHS)})[\s\u00a0]+(20\d{{2}})'
    rf'|(\d{{1,2}})[./](\d{{1,2}})[./](20\d{{2}}))\b'
)
TIME_RE = re.compile(r'(?i)\bore\s*(\d{1,2})[.:](\d{2})\b')
VENUE_RE = re.compile(
    r'(?i)\b(teatro\s+(?:palamostre|nuovo(?:\s+giovanni\s+da\s+udine)?)'
    r'|torre\s+di\s+s\.?\s*maria|cittadella\s+della\s+cultura'
    r'|sala\s+ajace|loggia\s+del\s+lionello|salone\s+del\s+popolo'
    r'|casa\s+della\s+confraternita|chiesa\s+di\s+[^\n,;]+)\b'
)


def clean_text(value):
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized_for_matching(text):
    return text.translate(str.maketrans('àèéìòùÀÈÉÌÒÙ', 'aeeiouAEEIOU'))


def event_segments(body):
    parsed = BeautifulSoup(str(body), 'html.parser')
    for node in parsed.select('br'):
        node.replace_with('__CRAWLER_NL__')
    for node in parsed.select('h1, h2, h3, h4, h5, h6, p, li, div'):
        node.insert_before('__CRAWLER_NL__')
        node.insert_after('__CRAWLER_NL__')
    text = clean_text(parsed.get_text('', strip=False).replace('__CRAWLER_NL__', '\n'))
    matching_text = normalized_for_matching(text)
    matches = list(DATE_RE.finditer(matching_text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield match, clean_text(text[match.start():end])


def find_venue(segment):
    match = VENUE_RE.search(segment)
    if not match:
        return None
    venue = re.sub(r'\s+', ' ', match.group(1)).strip(' ,.-')
    replacements = {
        'teatro palamostre': 'Teatro Palamostre',
        'torre di s. maria': 'Torre di Santa Maria',
        'torre di s maria': 'Torre di Santa Maria',
        'cittadella della cultura': 'Cittadella della Cultura',
        'teatro nuovo giovanni da udine': 'Teatro Nuovo Giovanni da Udine',
        'teatro nuovo': 'Teatro Nuovo Giovanni da Udine',
        'casa della confraternita': 'Casa della Confraternita',
        'sala ajace': 'Sala Ajace',
        'loggia del lionello': 'Loggia del Lionello',
        'salone del popolo': 'Salone del Popolo',
    }
    return replacements.get(venue.casefold(), venue)


def find_title(segment, date_match, venue):
    remainder = segment[date_match.end() - date_match.start():].strip(' *-\n')
    lines = [line.strip(' *-\t') for line in remainder.splitlines() if line.strip(' *-\t')]
    candidates = []
    venue_seen = False
    for line in lines:
        contains_venue = bool(VENUE_RE.search(line))
        line = re.split(r'(?i)\bmusich[ea]\s+di\b', line, maxsplit=1)[0]
        line = TIME_RE.sub('', line).strip(' ,.-')
        if venue:
            line = VENUE_RE.sub('', line).strip(' ,.-')
        was_after_venue = venue_seen
        venue_seen = venue_seen or contains_venue
        folded = normalized_for_matching(line).casefold()
        if not line or len(line) < 4:
            continue
        if folded.startswith(('musiche di', 'musica di', 'acquista', 'foto', 'mappa')):
            continue
        if folded in {'dal vivo', 'programma', 'concerto ert', 'tutti', '(tutti)',
                      'concerto in streaming per abbonati',
                      'concerti in streaming per abbonati'}:
            continue
        if folded.startswith('concerto cancellato'):
            continue
        candidates.append((line, was_after_venue))
    if not candidates:
        return None
    before_venue = [line for line, after in candidates if not after]
    chosen = before_venue[:2] if before_venue else [line for line, _ in candidates[:2]]
    title = ' '.join(chosen)
    return title[:500] or None


def parse_segment(match, segment, url):
    day, month_name, year, numeric_day, numeric_month, numeric_year = match.groups()
    try:
        if day:
            event_date = date(int(year), MONTHS[month_name.casefold()], int(day)).isoformat()
        else:
            event_date = date(int(numeric_year), int(numeric_month), int(numeric_day)).isoformat()
    except (KeyError, ValueError):
        return None

    folded = normalized_for_matching(segment).casefold()
    if 'streaming' in folded or 'coming soon' in folded or 'data da definire' in folded:
        return None
    venue = find_venue(segment)
    if url == TORRIANI_URL and '2023-06-01' <= event_date <= '2024-04-30':
        venue = 'Torre di Santa Maria'
    if not venue:
        return None
    title = find_title(segment, match, venue)
    if not title:
        return None

    time_match = TIME_RE.search(segment)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Udine',
        'country_code': 'IT',
        'description': segment,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AmicimusicaUdItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicimusica_ud_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue'],
    )

    def scrape(self):
        records = []
        for url in CALENDAR_URLS:
            try:
                response = requests.get(url, headers=HEADERS, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Amici della Musica Udine calendar',
                    event='crawler_fetch_failed', level='error', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.content, 'html.parser')
            body = soup.select_one('.field-name-body .field-item')
            if body is None:
                raise ValueError(f'Calendar body was not found at {url}')
            for match, segment in event_segments(body):
                record = parse_segment(match, segment, url)
                if record:
                    records.append(record)

        log_message(
            'Parsed Amici della Musica Udine calendar',
            event='crawler_scrape_completed',
            url=SOURCE_URL,
            record_count=len(records),
        )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    AmicimusicaUdItCrawler().run()


if __name__ == '__main__':
    main()
