import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sandrinepiau.com/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'Sandrine Piau'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_LINE_RE = re.compile(rf'\b(?:{MONTH_PATTERN})\b.*\b\d{{4}}\b', re.IGNORECASE)
DATE_GROUP_RE = re.compile(
    rf'(?P<days>\d{{1,2}}(?:er)?(?:\s*,\s*\d{{1,2}}(?:er)?)*(?:\s+et\s+\d{{1,2}}(?:er)?)?)'
    rf'\s+(?P<month>{MONTH_PATTERN})\b',
    re.IGNORECASE,
)

# The agenda is an international touring calendar. These are location clues
# actually used by the source; unknown locations are deliberately skipped.
LOCATION_OVERRIDES = {
    'Opéra Bastille, Paris': ('Opéra Bastille', 'Paris', 'FR'),
    'Mojo Club, Hamburg': ('Mojo Club', 'Hamburg', 'DE'),
    'Le prieuré clunisien de Froville': ('Le prieuré clunisien de Froville', 'Froville', 'FR'),
    'Cathédrale Saint-Pierre, Saintes': ('Cathédrale Saint-Pierre', 'Saintes', 'FR'),
    'Abbatiale de Lessay': ('Abbatiale de Lessay', 'Lessay', 'FR'),
    'Cinéma de Verbier, Suisse': ('Cinéma de Verbier', 'Verbier', 'CH'),
    'Chapelle de la Congrégation, Josselin': ('Chapelle de la Congrégation', 'Josselin', 'FR'),
    'Tuindorpkerk, Utrecht': ('Tuindorpkerk', 'Utrecht', 'NL'),
    'Château de Breteuil, Choisel': ('Château de Breteuil', 'Choisel', 'FR'),
    'Église Saint-Éloi des Mesnuls, Les Mesnuls': (
        'Église Saint-Éloi des Mesnuls', 'Les Mesnuls', 'FR'
    ),
    'Saline Royale, Arc-et-Senans': ('Saline Royale', 'Arc-et-Senans', 'FR'),
    'Théâtre du Châtelet, Paris': ('Théâtre du Châtelet', 'Paris', 'FR'),
    'Théâtre de Rungis, Rungis': ('Théâtre de Rungis', 'Rungis', 'FR'),
    'Théâtre La Criée, Marseille': ('Théâtre La Criée', 'Marseille', 'FR'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    text = ' '.join(text.replace('\xa0', ' ').replace('\u200b', '').split())
    # Wix occasionally splits a year across adjacent styled spans ("202 6").
    return re.sub(r'(?<=\d)\s+(?=\d)', '', text)


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def parse_dates(value):
    year_match = re.search(r'\b(20\d{2})\b', value)
    if not year_match:
        return []
    year = int(year_match.group(1))
    parsed = []
    for match in DATE_GROUP_RE.finditer(value):
        month = MONTHS[match.group('month').lower()]
        for day_text in re.findall(r'\d{1,2}', match.group('days')):
            try:
                parsed.append(date(year, month, int(day_text)).isoformat())
            except ValueError:
                log_message(
                    'Skipped invalid Sandrine Piau performance date',
                    event='crawler_item_skipped',
                    level='warning',
                    url=AGENDA_URL,
                    error_type='InvalidEventDate',
                    error_message=f'Invalid day {day_text} in published date line',
                )
    return parsed


def find_agenda_paragraphs(soup):
    candidates = []
    for container in soup.find_all(['div', 'section', 'main']):
        paragraphs = container.find_all('p', recursive=False)
        date_count = sum(bool(DATE_LINE_RE.search(clean_text(item))) for item in paragraphs)
        if date_count:
            candidates.append((date_count, paragraphs))
    return max(candidates, key=lambda item: item[0])[1] if candidates else []


def parse_event(block):
    values = [clean_text(item) for item in block]
    values = [value for value in values if value]
    if len(values) < 4:
        return []

    dates = parse_dates(values[0])
    location = LOCATION_OVERRIDES.get(values[1])
    link = next((item.find('a', href=True) for item in block if item.find('a', href=True)), None)
    url = canonical_url(link.get('href')) if link else ''
    detail_lines = [value for value in values[2:] if not value.startswith(('http://', 'https://'))]

    if not dates or not location or not url or not detail_lines:
        log_message(
            'Skipped incomplete Sandrine Piau performance',
            event='crawler_item_skipped',
            level='warning',
            url=url or AGENDA_URL,
            error_type='IncompleteEventData',
            error_message='Required date, city, venue, URL, or programme title is missing',
        )
        return []

    venue, city, country_code = location
    title = detail_lines[-2] if len(detail_lines) > 1 else detail_lines[0]
    description = '\n'.join(detail_lines)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class SandrinePiauComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sandrinepiau_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        response = requests.get(AGENDA_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        paragraphs = find_agenda_paragraphs(BeautifulSoup(response.text, 'html.parser'))
        if not paragraphs:
            raise RuntimeError('Could not locate dated programme entries on the agenda page')

        blocks = []
        current = []
        for paragraph in paragraphs:
            if DATE_LINE_RE.search(clean_text(paragraph)):
                if current:
                    blocks.append(current)
                current = [paragraph]
            elif current:
                current.append(paragraph)
        if current:
            blocks.append(current)

        records = [record for block in blocks for record in parse_event(block)]
        return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


def main():
    SandrinePiauComCrawler().run()


if __name__ == '__main__':
    main()
