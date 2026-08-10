import html
import json
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.knechtsteden.com/'
SOURCE = 'Festival Alte Musik Knechtsteden'
ARCHIVE_URL = 'https://2023.knechtsteden.com/programm/'
PROGRAMME_PREFIX = '/programm/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'märz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}

# The programme uses a small, stable set of festival venues. The cities below
# are confirmed by the site's Spielstätten page and the venues' postal addresses.
VENUE_CITIES = {
    'klosterbasilika knechtsteden': 'Dormagen',
    'klosterbibliothek knechtsteden': 'Dormagen',
    'kulturhof knechsteden (bullenstall)': 'Dormagen',
    'kulturhof knechtsteden (bullenstall)': 'Dormagen',
    'kreismuseum zons': 'Dormagen',
    'schloss arff (festsaal)': 'Köln',
    'globe neuss': 'Neuss',
    'globe theater neuss': 'Neuss',
    'schloss arff': 'Köln',
}

NON_EVENT_PATHS = {
    '/programm/programmkalender',
    '/programm/movimento',
    '/programm/junges-festival',
}

DATE_RE = re.compile(
    r'\b(\d{1,2})\.\s*('
    + '|'.join(MONTHS)
    + r')\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'^(?:[01]?\d|2[0-3]):[0-5]\d$')
SEARCH_INDEX_RE = re.compile(
    r'https://framerusercontent\.com/sites/[^"\s]+/searchIndex-[^"\s]+\.json'
)


def clean_text(value):
    value = html.unescape(str(value or ''))
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def find_city(venue):
    normalized = clean_text(venue).lower()
    return VENUE_CITIES.get(normalized)


def is_price_text(value):
    lowered = value.lower()
    return '€' in value or 'vvk-gebühr' in lowered or lowered.startswith('teilnahmegebühr')


def build_title(entry, paragraphs, date_index):
    primary = clean_text((entry.get('h1') or [''])[0])
    subtitle = ''
    for value in reversed(paragraphs[:date_index]):
        lowered = value.lower()
        if (
            not value
            or value == 'Menü'
            or '©' in value
            or 'ausverkauft' in lowered
            or lowered.startswith('restkarten')
        ):
            continue
        subtitle = value
        break

    if subtitle and subtitle.casefold() != primary.casefold():
        return f'{primary} – {subtitle}' if primary else subtitle
    return primary or subtitle


def parse_entry(path, entry):
    paragraphs = [clean_text(value) for value in entry.get('p', [])]
    date_index = next(
        (index for index, value in enumerate(paragraphs) if DATE_RE.search(value)), None
    )
    if date_index is None:
        return None

    event_date = parse_date(paragraphs[date_index])
    time_index = next(
        (
            index
            for index in range(date_index + 1, len(paragraphs))
            if TIME_RE.fullmatch(paragraphs[index])
        ),
        None,
    )
    if not event_date or time_index is None:
        return None

    venue_index = time_index + 1
    while venue_index < len(paragraphs) and (
        paragraphs[venue_index] in {'Uhr', '–', '-'}
        or TIME_RE.fullmatch(paragraphs[venue_index])
    ):
        venue_index += 1
    if venue_index >= len(paragraphs):
        return None

    venue = paragraphs[venue_index]
    city = find_city(venue)
    title = build_title(entry, paragraphs, date_index)
    if not title or not city:
        return None

    # Workshops are listed beside concerts but are not concert performances.
    if title.casefold().startswith('workshop'):
        return None

    description_parts = []
    for value in paragraphs[venue_index + 1:]:
        if value.lower().startswith('zurück zur übersicht'):
            break
        if value and not is_price_text(value):
            description_parts.append(value)

    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': paragraphs[time_index],
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_archive_item(item):
    link = item.select_one('a[href*="/veranstaltungen/"]')
    heading = link.select_one('.head') if link else None
    date_element = item.select_one('.item-group-head')
    info = item.select_one('.info')
    if not link or not heading or not date_element or not info:
        return None

    title_parts = [clean_text(value) for value in heading.stripped_strings]
    title = ' – '.join(value for value in title_parts if value)
    event_date = parse_date(f'{clean_text(date_element.get_text(" ", strip=True))} 2023')
    info_text = clean_text(info.get_text(' ', strip=True))
    time_match = re.match(r'(\d{1,2}:\d{2})(?:\s*-\s*\d{1,2}:\d{2})?\s+Uhr\s*-\s*', info_text)
    if not title or not event_date or not time_match:
        return None

    title_position = info_text.find(title_parts[0], time_match.end())
    if title_position < 0:
        return None
    venue = clean_text(info_text[time_match.end():title_position])
    city = find_city(venue)
    if not city:
        return None

    quickinfo = item.select_one('.quickinfo')
    description = clean_text(quickinfo.get_text('\n', strip=True)) if quickinfo else ''
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(ARCHIVE_URL, link.get('href', '')),
        'time_from': time_match.group(1),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class KnechtstedenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='knechtsteden_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SOURCE_URL, timeout=45)
            response.raise_for_status()
            index_urls = list(dict.fromkeys(SEARCH_INDEX_RE.findall(response.text)))
            if not index_urls:
                raise ValueError('Could not discover the Framer search index')

            index_response = session.get(index_urls[0], timeout=45)
            index_response.raise_for_status()
            search_index = index_response.json()
            if not isinstance(search_index, dict):
                raise ValueError('Unexpected Framer search index format')

            archive_response = session.get(ARCHIVE_URL, timeout=45)
            archive_response.raise_for_status()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as error:
            log_message(
                'Failed to fetch Knechtsteden programme',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for path, entry in search_index.items():
            if (
                not path.startswith(PROGRAMME_PREFIX)
                or path in NON_EVENT_PATHS
                or not isinstance(entry, dict)
            ):
                continue
            record = parse_entry(path, entry)
            if record:
                records.append(record)

        archive_soup = BeautifulSoup(archive_response.text, 'html.parser')
        for item in archive_soup.select('div.item'):
            record = parse_archive_item(item)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    KnechtstedenComCrawler().run()


if __name__ == '__main__':
    main()
