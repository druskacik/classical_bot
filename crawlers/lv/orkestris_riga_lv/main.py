import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orkestris.riga.lv/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Orķestris RĪGA'
CONCERT_CATEGORY_ID = 4

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'lv-LV,lv;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'janv': 1, 'janvāris': 1, 'janvāri': 1,
    'feb': 2, 'februāris': 2, 'februārī': 2,
    'mar': 3, 'marts': 3, 'martā': 3,
    'apr': 4, 'aprīlis': 4, 'aprīlī': 4,
    'mai': 5, 'maijs': 5, 'maijā': 5,
    'jun': 6, 'jūn': 6, 'jūnijs': 6, 'jūnijā': 6,
    'jul': 7, 'jūlijs': 7, 'jūlijā': 7,
    'aug': 8, 'augusts': 8, 'augustā': 8,
    'sep': 9, 'sept': 9, 'sepembris': 9, 'sepembrī': 9,
    'septembris': 9, 'septembrī': 9,
    'okt': 10, 'oktobris': 10, 'oktobrī': 10,
    'nov': 11, 'novembris': 11, 'novembrī': 11,
    'dec': 12, 'decembris': 12, 'decembrī': 12,
}

NON_RIGA_LOCATIONS = {
    'cēs': 'Cēsis',
    'kuldīg': 'Kuldīga',
    'liepāj': 'Liepāja',
    'limbaž': 'Limbaži',
    'ogr': 'Ogre',
    'saulkrast': 'Saulkrasti',
    'siguld': 'Sigulda',
    'tukum': 'Tukums',
    'valka': 'Valka',
    'ventspil': 'Ventspils',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event_date(text, published_at):
    header = '\n'.join(text.splitlines()[:25])
    patterns = (
        r'(?:(20\d{2})\.\s*(?:gada|g\.)\s+)?(\d{1,2})\.\s*([A-Za-zĀ-ž]+)',
        r'(\d{1,2})\.\s*([A-ZĀČĒĢĪĶĻŅŠŪŽ]{3,})\b',
    )
    match = re.search(patterns[0], header, re.I)
    explicit_year = None
    if match:
        explicit_year, day, month_name = match.groups()
    else:
        match = re.search(patterns[1], header)
        if not match:
            return None
        day, month_name = match.groups()
    month = MONTHS.get(month_name.lower().rstrip('.'))
    if not month:
        return None
    published = date.fromisoformat(published_at[:10])
    year = int(explicit_year) if explicit_year else published.year
    # Concerts announced late in a year for the following January-May are
    # common; the WordPress publication timestamp preserves that distinction.
    if not explicit_year and month < published.month - 4:
        year += 1
    try:
        return date(year, month, int(day)).isoformat()
    except ValueError:
        return None


def parse_times(text):
    lines = text.splitlines()[:30]
    for index, line in enumerate(lines):
        if re.fullmatch(r'(?:Norises vieta|Place|Location)\s*:?', line.strip(), re.I):
            lines = lines[:index]
            break
    header = '\n'.join(lines)
    values = re.findall(r'(?<!\d)([012]?\d)[.:](\d{2})(?!\d)', header)
    times = []
    for hour, minute in values:
        if int(hour) < 24 and int(minute) < 60:
            value = f'{int(hour):02d}:{minute}'
            if value not in times:
                times.append(value)
    return times or [None]


def parse_venue(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if re.fullmatch(r'(?:Norises vieta|Place|Location)\s*:?', line, re.I) and index + 1 < len(lines):
            venue = lines[index + 1]
            if re.fullmatch(r'\d{1,2}\.\s*[A-ZĀ-Ž]+', venue) and index + 2 < len(lines):
                venue = lines[index + 2]
            venue = re.sub(r'\s*\([^)]*(?:bezmaksas|ielūgum|biļe)[^)]*\)\s*$', '', venue, flags=re.I)
            venue = re.sub(r'\s*,?\s*(?:Ropažu|Rīgas) iela \d+.*$', '', venue, flags=re.I)
            if re.search(r'tiks precizēts|tiešsaist|tiešraid', venue, re.I):
                return None
            return venue.strip(' ,') or None
    return None


def infer_location(venue, title):
    evidence = f'{venue}\n{title}'
    if re.search(r'viļņ|vilnius', evidence, re.I):
        return 'Vilnius', 'LT'
    if re.search(r'tallin', evidence, re.I):
        return 'Tallinn', 'EE'
    for marker, city in NON_RIGA_LOCATIONS.items():
        if marker in evidence.lower():
            return city, 'LV'
    # This is Riga's municipal orchestra and all unmarked venues in its own
    # calendar are identifiable Riga institutions. Touring venues name their
    # municipality in either the venue or title.
    return 'Rīga', 'LV'


def parse_post(post):
    url = clean_text(post.get('link'))
    title = clean_text(post.get('title', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    event_date = parse_event_date(description, post.get('date', ''))
    venue = parse_venue(description)
    if not url or not title or not event_date or not venue:
        return []
    city, country_code = infer_location(venue, title)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in parse_times(description)
    ]


class OrkestrisRigaLvCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orkestris_riga_lv',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LV',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            response = requests.get(
                API_URL,
                params={
                    'categories': CONCERT_CATEGORY_ID,
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'asc',
                    '_fields': 'date,link,title,content',
                },
                headers=HEADERS,
                timeout=60,
            )
            response.raise_for_status()
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            posts = response.json()
            for post in posts:
                parsed = parse_post(post)
                if parsed:
                    records.extend(parsed)
                else:
                    log_message(
                        'Skipped incomplete Orķestris RĪGA concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(post.get('link')),
                        error_type='IncompleteEventData',
                        error_message='Required date, title, venue, or city is missing',
                    )
            page += 1
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    OrkestrisRigaLvCrawler().run()


if __name__ == '__main__':
    main()
