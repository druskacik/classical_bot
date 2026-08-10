import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jovenorquestadejaen.es/'
SOURCE = 'Joven Orquesta de Jaén'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'septiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}

VENUE_PATTERNS = (
    (r'Paraninfo del (?:CPM|Conservatorio Profesional de M[uú]sica)(?: Ram[oó]n Garay)? de Ja[eé]n',
     'Paraninfo del CPM Ramón Garay'),
    (r'(?:Nuevo )?Teatro Infant[ae] Leonor(?: de Ja[eé]n)?', 'Teatro Infanta Leonor'),
    (r'Teatro Darymelia', 'Teatro Darymelia'),
)


def clean_text(value):
    if value is None:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = html.unescape(soup.get_text(' ', strip=True))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(text, published_at, modified_at):
    match = re.search(
        r'\b(\d{1,2})\s+de\s+'
        r'(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)'
        r'(?:\s+de\s+(\d{4}))?\b',
        text,
        re.IGNORECASE,
    )
    if not match:
        return None

    # Undated announcements refer to their publication year. The March 2020
    # concert post was explicitly updated a year later and says it is finally
    # taking place "un año después", so its modification year is authoritative.
    published = datetime.fromisoformat(published_at).date()
    modified = datetime.fromisoformat(modified_at).date()
    year = int(match.group(3)) if match.group(3) else published.year
    if not match.group(3) and modified.year > published.year:
        year = modified.year
    try:
        return date(year, MONTHS[match.group(2).lower()], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\b(?:a\s+las\s+)?(\d{1,2})[.:]([0-5]\d)\s*(?:horas?)?\b', text, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    if hour > 23:
        return None
    return f'{hour:02d}:{match.group(2)}'


def parse_venue(text):
    for pattern, venue in VENUE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return venue
    return None


def parse_post(post):
    title = clean_text((post.get('title') or {}).get('rendered'))
    description = clean_text((post.get('content') or {}).get('rendered'))
    combined = f'{title} {description}'

    # Recaps use past-tense language and do not provide a future event listing.
    if re.search(r'\b(?:el pasado|ha participado|rueda de prensa)\b', combined, re.IGNORECASE):
        return None

    event_date = parse_date(combined, post.get('date', ''), post.get('modified', ''))
    venue = parse_venue(combined)
    url = post.get('link')
    if not all((title, event_date, url, venue)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(combined),
        'venue': venue,
        'city': 'Jaén',
        'country_code': 'ES',
        'description': description or None,
    }


class JovenOrquestaDeJaenEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jovenorquestadejaen_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 100,
                        'page': page,
                        '_fields': 'date,modified,link,title,content',
                    },
                    timeout=60,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Joven Orquesta de Jaén archive',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for post in response.json():
                record = parse_post(post)
                if record:
                    records.append(record)

            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    JovenOrquestaDeJaenEsCrawler().run()


if __name__ == '__main__':
    main()
