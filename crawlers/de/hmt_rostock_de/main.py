import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hmt-rostock.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'veranstaltungen/veranstaltungskalender/')
SOURCE = 'Hochschule für Musik und Theater Rostock'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTH_SLUGS = (
    'januar', 'februar', 'maerz', 'april', 'mai', 'juni',
    'juli', 'august', 'september', 'oktober', 'november', 'dezember',
)

# Rooms without a city in their displayed name are rooms in the university's
# main building in Rostock. Explicitly named touring venues are handled below.
HOME_VENUE_TERMS = (
    'katharinensaal', 'kammermusiksaal', 'orgelsaal', 'theater im katharinenstift',
    'foyer', 'kapitelsaal', 'tonstudio', 'hochschule für musik und theater',
)
VENUE_CITIES = {
    'rostock': 'Rostock',
    'neubrandenburg': 'Neubrandenburg',
    'schwerin': 'Schwerin',
    'stralsund': 'Stralsund',
    'greifswald': 'Greifswald',
    'guestrow': 'Güstrow',
    'güstrow': 'Güstrow',
    'wismar': 'Wismar',
    'berlin': 'Berlin',
    'hamburg': 'Hamburg',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_url(year, month):
    return urljoin(CALENDAR_URL, f'{year}/{MONTH_SLUGS[month - 1]}/')


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    # TYPO3 removes elapsed events from month listings, including old archive
    # URLs. Scan two years ahead so events published well in advance are kept.
    today = date.today()
    urls = set()
    month_urls = []
    for offset in range(25):
        month_index = today.month - 1 + offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        month_urls.append(month_url(year, month))
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in month_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                soup = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for link in soup.select('.calendar-month-list a.url[href], .cal-list__item a[href]'):
                href = link.get('href', '')
                if '/veranstaltungen/veranstaltungskalender/e/n/' in href:
                    urls.add(urljoin(SOURCE_URL, href))
    return sorted(urls)


def resolve_city(venue, title):
    location_text = f'{venue} {title}'.lower()
    for term, city in VENUE_CITIES.items():
        if term in location_text:
            return city
    if any(term in venue.lower() for term in HOME_VENUE_TERMS):
        return 'Rostock'
    return None


def parse_detail(soup, url):
    main = soup.select_one('main')
    if not main:
        return None
    title_node = main.select_one('h1')
    time_node = main.select_one('time')
    if not title_node or not time_node:
        return None

    title = clean_text(title_node)
    datetime_value = time_node.get('datetime', '')
    event_date = ''
    time_from = None
    match = re.match(r'(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?', datetime_value)
    if match:
        event_date = match.group(1)
        if match.group(2):
            time_from = f'{match.group(2)}:{match.group(3)}'
    else:
        text_match = re.search(r'(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+).*?(\d{2}):(\d{2})', clean_text(time_node))
        year_match = re.search(r'/veranstaltungskalender/(\d{4})/', url)
        month_names = {name: index + 1 for index, name in enumerate(MONTH_SLUGS)}
        if text_match and year_match:
            month = month_names.get(text_match.group(2).lower().replace('märz', 'maerz'))
            if month:
                try:
                    event_date = date(int(year_match.group(1)), month, int(text_match.group(1))).isoformat()
                    time_from = f'{text_match.group(3)}:{text_match.group(4)}'
                except ValueError:
                    pass
    if not time_from:
        clock_match = re.search(r'(\d{1,2}):(\d{2})\s*Uhr', clean_text(time_node))
        if clock_match:
            time_from = f'{int(clock_match.group(1)):02d}:{clock_match.group(2)}'
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except ValueError:
        return None

    venue = ''
    label = main.find(string=lambda value: value and 'Veranstaltungsort:' in value)
    if label:
        container = label.parent
        venue_node = container.find_next('li')
        venue = clean_text(venue_node)
    if not venue:
        info = time_node.parent.get_text(' ', strip=True)
        if '|' in info:
            venue = clean_text(info.split('|', 1)[1])

    city = resolve_city(venue, title) if venue else None
    # Detail pages sometimes append the postal address to the venue list item.
    # The database venue field should contain the place name only.
    venue = venue.split('\n', 1)[0].strip()
    if not title or not venue or not city:
        return None

    description_parts = []
    for node in title_node.find_all_next(['p', 'ul', 'ol']):
        if node.find_parent('footer') or node.find_previous('hr'):
            break
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    description = clean_text('\n\n'.join(description_parts)) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_detail(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class HmtRostockDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hmt_rostock_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
    HmtRostockDeCrawler().run()


if __name__ == '__main__':
    main()
