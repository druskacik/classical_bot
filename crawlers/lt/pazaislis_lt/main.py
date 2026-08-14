import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pazaislis.lt/'
SOURCE = 'Pažaislio muzikos festivalis'
PROGRAM_URL = urljoin(SOURCE_URL, 'programa/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'festivalis/archyvas/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Upgrade-Insecure-Requests': '1',
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
}

MONTHS = {
    'sausio': 1, 'vasario': 2, 'kovo': 3, 'balandžio': 4,
    'gegužės': 5, 'birželio': 6, 'liepos': 7, 'rugpjūčio': 8,
    'rugsėjo': 9, 'spalio': 10, 'lapkričio': 11, 'gruodžio': 12,
}

# Venues are spread around Lithuania. These stems appear in the site's own
# venue names and are strong enough to identify the municipality/city.
CITY_MARKERS = (
    ('Kauno', 'Kaunas'), ('Pažaislio', 'Kaunas'), ('Zapyšk', 'Zapyškis'),
    ('Raudondvar', 'Raudondvaris'), ('Pakruoj', 'Pakruojis'),
    ('Šiauli', 'Šiauliai'), ('Ignalinos', 'Ignalina'), ('Paliesiaus', 'Mielagėnai'),
    ('Molėtų', 'Molėtai'), ('Dubingių', 'Dubingiai'), ('Rietavo', 'Rietavas'),
    ('Birštono', 'Birštonas'), ('Rumšiški', 'Rumšiškės'), ('Žeimių', 'Žeimiai'),
    ('Žiežmari', 'Žiežmariai'), ('Margininkų', 'Margininkai'),
    ('Elektrėn', 'Elektrėnai'), ('Kaišiador', 'Kaišiadorys'), ('Žaslių', 'Žasliai'),
    ('Gelgaudišk', 'Gelgaudiškis'), ('Zyplių', 'Lukšiai'), ('Pociūnų', 'Pociūnai'),
    ('Babtyn', 'Žemaitkiemis'), ('Jonavos', 'Jonava'), ('Klaipėd', 'Klaipėda'),
    ('Vilkavišk', 'Vilkaviškis'), ('Kulautuv', 'Kulautuva'), ('Jiezno', 'Jieznas'),
    ('Obelynės', 'Akademija'), ('Anykščių', 'Anykščiai'), ('Prienų', 'Prienai'),
    ('Zarembų', 'Daugailiai'), ('Žemosios Panemunės', 'Žemoji Panemunė'),
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(soup):
    return {
        urljoin(SOURCE_URL, link['href'])
        for article in soup.select('.events-list article, article.event-item')
        if (link := article.find('a', href=True))
    }


def archive_ids(soup):
    ids = set()
    for link in soup.select('a[href*="years="]'):
        match = re.search(r'[?&]years=(\d+)', link.get('href', ''))
        if match:
            ids.add(match.group(1))
    return ids


def parse_datetime(value):
    text = clean_text(value).lower()
    match = re.search(
        r'(20\d{2})\s*m\.\s*([a-ząčęėįšųūž]+)\s+(\d{1,2})\s*d\.\s*'
        r'(?:(\d{1,2}):(\d{2}))?',
        text,
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    year, month_name, day, hour, minute = match.groups()
    try:
        date = f'{int(year):04d}-{MONTHS[month_name]:02d}-{int(day):02d}'
        # Round-trip validation rejects impossible dates.
        from datetime import date as date_type
        date_type.fromisoformat(date)
    except ValueError:
        return None, None
    time_from = f'{int(hour):02d}:{int(minute):02d}' if hour is not None else None
    return date, time_from


def resolve_city(venue):
    for marker, city in CITY_MARKERS:
        if marker.casefold() in venue.casefold():
            return city
    return None


def event_description(entry):
    copy = BeautifulSoup(str(entry), 'html.parser')
    for node in copy.select('.price, script, style, .event-btns'):
        node.decompose()
    heading = copy.find(['h1', 'h2'])
    if heading:
        heading.decompose()
    return clean_text(copy) or None


def parse_event(soup, url):
    detail = soup.select_one('.event-detail')
    entry = soup.select_one('.text-box .entry')
    if not detail or not entry:
        return None
    strong = detail.find('strong')
    date, time_from = parse_datetime(strong)
    title_node = entry.find(['h1', 'h2'])
    title = clean_text(title_node)
    venue = clean_text(' '.join(
        clean_text(sibling) for sibling in strong.next_siblings
    )) if strong else ''
    city = resolve_city(venue)
    if not all((title, date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'LT',
        'description': event_description(entry),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        programme = get_soup(session, PROGRAM_URL, params={'cat': '1'})
        archive = get_soup(session, ARCHIVE_URL)
    except requests.RequestException as error:
        log_message(
            'Failed to load festival listings', event='crawler_feed_failed',
            level='warning', url=SOURCE_URL, error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    urls = listing_urls(programme)
    year_ids = archive_ids(archive)
    urls.update(listing_urls(archive))
    for year_id in sorted(year_ids):
        try:
            urls.update(listing_urls(get_soup(session, ARCHIVE_URL, {'years': year_id})))
        except requests.RequestException as error:
            log_message(
                'Failed to load festival archive', event='crawler_feed_failed',
                level='warning', url=f'{ARCHIVE_URL}?years={year_id}',
                error_type=type(error).__name__, error_message=str(error),
            )

    records = []
    for url in sorted(urls):
        try:
            record = parse_event(get_soup(session, url), url)
        except requests.RequestException as error:
            log_message(
                'Failed to load event detail', event='crawler_event_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PazaislisLtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pazaislis_lt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    PazaislisLtCrawler().run()


if __name__ == '__main__':
    main()
