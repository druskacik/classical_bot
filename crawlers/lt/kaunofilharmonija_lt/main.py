import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kaunofilharmonija.lt/'
SOURCE = 'Kauno valstybinė filharmonija'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
HOME_VENUE = 'Kauno valstybinė filharmonija'
HOME_CITY = 'Kaunas'
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'lt-LT,lt;q=0.9,en;q=0.7',
}

# Locations outside the home hall are normally stated in the first paragraph.
# Match the inflected Lithuanian forms used by the site, longest names first.
CITY_MARKERS = (
    ('Marijampol', 'Marijampolė'),
    ('Druskinink', 'Druskininkai'),
    ('Anykšč', 'Anykščiai'),
    ('Kėdain', 'Kėdainiai'),
    ('Klaipėd', 'Klaipėda'),
    ('Panevėž', 'Panevėžys'),
    ('Šiaul', 'Šiauliai'),
    ('Palang', 'Palanga'),
    ('Rietav', 'Rietavas'),
    ('Biršton', 'Birštonas'),
    ('Jonav', 'Jonava'),
    ('Zapyšk', 'Zapyškis'),
    ('Raudondvar', 'Raudondvaris'),
    ('Garliav', 'Garliava'),
    ('Vilni', 'Vilnius'),
    ('Kaun', 'Kaunas'),
)

VENUE_ALIASES = {
    'kauno valstybinėje filharmonijoje': HOME_VENUE,
    'kauno valstybinė filharmonija': HOME_VENUE,
    'senojoje zapyškio bažnyčioje': 'Senoji Zapyškio bažnyčia',
    'kauno kristaus prisikėlimo bažnyčioje': 'Kauno Kristaus Prisikėlimo bažnyčia',
    'raudondvario dvaro menų inkubatoriaus salėje': 'Raudondvario dvaro menų inkubatorius',
    'rietavo kultūros centre': 'Rietavo kultūros centras',
    'didžiojoje salėje': f'{HOME_VENUE}, Didžioji salė',
    'mažojoje salėje': f'{HOME_VENUE}, Mažoji salė',
}

LOCATION_WORDS = re.compile(
    r'\b(filharmonij|bažnyč|bazilik|katedr|sal(?:ė|ėje)|centr|teatr|rūm|'
    r'dvar|muziej|bibliotek|aren|koncertų nam)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': PAGE_SIZE, 'page': page, 'orderby': 'date', 'order': 'desc'},
            timeout=60,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages or not batch:
            break
        page += 1
    return posts


def first_paragraph(content_html):
    soup = BeautifulSoup(content_html or '', 'html.parser')
    for element in soup.find_all(['p', 'div']):
        text = clean_text(element)
        if text:
            return text.split('\n', 1)[0].strip()
    return ''


def city_from_text(text):
    folded = text.casefold()
    for marker, city in CITY_MARKERS:
        if marker.casefold() in folded:
            return city
    return None


def resolve_location(content_html):
    first = first_paragraph(content_html)
    # Venue headings are short standalone paragraphs. A long introductory
    # paragraph is description, not a location field.
    if first and len(first) <= 180 and LOCATION_WORDS.search(first):
        key = first.strip(' .:').casefold()
        if key in ('didžiojoje salėje', 'mažojoje salėje'):
            return VENUE_ALIASES[key], HOME_CITY
        city = city_from_text(first)
        if city:
            return VENUE_ALIASES.get(key, first.strip(' .:')), city
        # An explicitly named location cannot safely inherit the home city.
        return None, None
    return HOME_VENUE, HOME_CITY


def make_record(post):
    title = clean_text((post.get('title') or {}).get('rendered'))
    url = post.get('link') or ''
    content_html = (post.get('content') or {}).get('rendered') or ''
    venue, city = resolve_location(content_html)
    try:
        starts_at = datetime.fromisoformat(post.get('date') or '')
    except (TypeError, ValueError):
        return None
    if not title or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'LT',
        'description': clean_text(content_html) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        posts = get_posts(session)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to scrape concert feed',
            event='crawler_feed_failed',
            level='warning',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    records = [record for post in posts if (record := make_record(post))]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class KaunofilharmonijaLtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kaunofilharmonija_lt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LT',
        upload_target='potential',
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
        return get_concerts()


def main():
    KaunofilharmonijaLtCrawler().run()


if __name__ == '__main__':
    main()
