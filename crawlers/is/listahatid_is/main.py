import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.listahatid.is/'
EVENTS_URL = urljoin(SOURCE_URL, 'vidburdir')
SOURCE = 'Listahátíð í Reykjavík'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'is,en-GB;q=0.9,en;q=0.8',
}

# This is a multidisciplinary festival. These are first-party Prismic
# categories, combined so opera and potentially qualifying dance are not lost.
CANDIDATE_CATEGORIES = {'Tónlist', 'Ópera', 'Dans'}

MONTHS = {
    'janúar': 1, 'jan': 1,
    'febrúar': 2, 'feb': 2,
    'mars': 3, 'mar': 3,
    'apríl': 4, 'apr': 4,
    'maí': 5,
    'júní': 6, 'jún': 6,
    'júlí': 7, 'júl': 7,
    'ágúst': 8, 'ágú': 8,
    'september': 9, 'sep': 9,
    'október': 10, 'okt': 10,
    'nóvember': 11, 'nóv': 11,
    'desember': 12, 'des': 12,
}

CITY_HINTS = (
    (r'akureyri', 'Akureyri'),
    (r'eskifjar', 'Eskifjörður'),
    (r'sel[aá]rdal', 'Selárdalur'),
    (r'dj[uú]pav[ií]k', 'Djúpavík'),
    (r'reykjav[ií]k|harpa|h[oö]rputorg|eldborg|nor[ðd]urlj[oó]s|silfurberg|hallgr[ií]mskirkja|'
     r'i[ðd]n[oó]|tjarnarb[ií][oó]|borgarleikh[uú]si[ðd]|austurb[æa]jarb[ií][oó]|'
     r'elli[ðd]a[aá]rst[oö][ðd]|norr[æa]na h[uú]si[ðd]|[þt]j[oó][ðd]minjasafni[ðd]',
     'Reykjavík'),
)

DATE_RE = re.compile(
    r'(?<!\d)(\d{1,2})\.\s*'
    r'(jan(?:úar)?|feb(?:rúar)?|mars?|apr(?:íl)?|maí|jún(?:í)?|júl(?:í)?|'
    r'ágú(?:st)?|sep(?:tember)?|okt(?:óber)?|nóv(?:ember)?|des(?:ember)?)'
    r'(?:\s+(20\d{2}))?'
    r'(?:\s+(?:kl\.?\s*)?(\d{1,2})[:.]([0-5]\d))?',
    re.IGNORECASE,
)


def rich_text(value):
    if not value:
        return ''
    if isinstance(value, list):
        parts = [item.get('text', '') for item in value if isinstance(item, dict)]
        value = '\n'.join(parts)
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    response = session.get(EVENTS_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    node = soup.select_one('#__NEXT_DATA__')
    if not node:
        raise ValueError('Next.js data payload was not found')
    payload = json.loads(node.get_text())
    return payload.get('props', {}).get('pageProps', {}).get('events', [])


def infer_city(location):
    folded = location.casefold()
    for pattern, city in CITY_HINTS:
        if re.search(pattern, folded, re.IGNORECASE):
            return city
    return None


def parse_occurrences(data, start):
    occurrences = {(start.date().isoformat(), start.strftime('%H:%M'))}
    details = rich_text(data.get('date_details'))
    for match in DATE_RE.finditer(details):
        day, month_name, year, hour, minute = match.groups()
        month = MONTHS.get(month_name.casefold())
        if not month:
            continue
        try:
            value = datetime(int(year or start.year), month, int(day))
        except ValueError:
            continue
        time_from = f'{int(hour):02d}:{minute}' if hour is not None else None
        occurrences.add((value.date().isoformat(), time_from))
        # Some detail rows list two same-day performances as "15:30 & 17:00".
        line_tail = details[match.end():].split('\n', 1)[0]
        for extra_hour, extra_minute in re.findall(r'&\s*(\d{1,2}):([0-5]\d)', line_tail):
            occurrences.add(
                (value.date().isoformat(), f'{int(extra_hour):02d}:{extra_minute}')
            )
    return sorted(occurrences)


def parse_event(item):
    data = item.get('data') or {}
    categories = {
        entry.get('category') for entry in data.get('category_list', [])
        if isinstance(entry, dict)
    }
    if not categories.intersection(CANDIDATE_CATEGORIES):
        return []

    title = rich_text(data.get('title'))
    location = rich_text(data.get('location'))
    start_value = data.get('start_date')
    event_path = item.get('url')
    if not title or not location or not start_value or not event_path:
        return []
    try:
        start = datetime.fromisoformat(start_value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return []

    city = infer_city(location)
    # A city-only location is not a defensible venue.
    if not city or location.casefold().strip() == city.casefold():
        return []

    description_parts = [
        rich_text(data.get('summary')),
        rich_text(data.get('content')),
        rich_text(data.get('body')),
        rich_text(data.get('date_details')),
        '\n'.join(
            rich_text(artist.get('artist_name'))
            for artist in data.get('artists', []) if isinstance(artist, dict)
        ),
    ]
    description = '\n\n'.join(
        part for index, part in enumerate(description_parts)
        if part and part not in description_parts[:index]
    ) or None
    url = urljoin(SOURCE_URL, event_path)

    return [
        {
            'title': title,
            'date': date_value,
            'url': url,
            'time_from': time_from if data.get('show_start_time', True) else None,
            'venue': location,
            'city': city,
            'country_code': 'IS',
            'description': description,
        }
        for date_value, time_from in parse_occurrences(data, start)
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    records = []
    for item in events:
        try:
            records.extend(parse_event(item))
        except (AttributeError, TypeError, ValueError) as error:
            log_message(
                'Skipping malformed festival event',
                event='crawler_event_parse_failed',
                level='warning',
                url=urljoin(SOURCE_URL, item.get('url') or ''),
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class ListahatidIsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='listahatid_is',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IS',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ListahatidIsCrawler().run()


if __name__ == '__main__':
    main()
