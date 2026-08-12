import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://capellasavaria.hu/'
CONCERT_ARCHIVE_URL = urljoin(SOURCE_URL, 'category/koncert/')
SOURCE = 'Capella Savaria'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'hu-HU,hu;q=0.9,en;q=0.7',
}

MONTHS = {
    'január': 1, 'február': 2, 'március': 3, 'április': 4,
    'május': 5, 'június': 6, 'július': 7, 'augusztus': 8,
    'szeptember': 9, 'október': 10, 'november': 11, 'december': 12,
}

# Foreign tour locations occasionally appear without a country. Keep the
# inference deliberately small; unknown locations remain Hungarian only when
# the page gives no evidence that this Hungarian ensemble is touring abroad.
FOREIGN_CITIES = {
    'bécs': ('Bécs', 'AT'),
    'vienna': ('Vienna', 'AT'),
    'pozsony': ('Pozsony', 'SK'),
    'bratislava': ('Bratislava', 'SK'),
    'prága': ('Prága', 'CZ'),
    'prague': ('Prague', 'CZ'),
    'zágráb': ('Zágráb', 'HR'),
    'zagreb': ('Zagreb', 'HR'),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def parse_datetime(value):
    text = clean_text(value).casefold()
    match = re.search(
        r'(?P<year>20\d{2})\.\s*(?P<month>[a-záéíóöőúüű]+)\s+'
        r'(?P<day>\d{1,2})\.?(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?',
        text,
    )
    if not match or match.group('month') not in MONTHS:
        return None
    try:
        event_date = date(
            int(match.group('year')),
            MONTHS[match.group('month')],
            int(match.group('day')),
        ).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group('hour') is not None:
        hour, minute = int(match.group('hour')), int(match.group('minute'))
        if hour > 23 or minute > 59:
            return None
        time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def parse_location(value):
    location = clean_text(value)
    if not location:
        return None

    # Event pages consistently use "venue, CITY"; the archive uses an en dash.
    parts = re.split(r'\s*[–—]\s*|\s*,\s*', location)
    parts = [part.strip(' .') for part in parts if part.strip(' .')]
    if len(parts) < 2:
        return None
    venue = ', '.join(parts[:-1])
    raw_city = parts[-1]
    if not venue or not raw_city or venue.casefold() == raw_city.casefold():
        return None

    city_key = raw_city.casefold()
    city, country_code = FOREIGN_CITIES.get(
        city_key, (raw_city.title() if raw_city.isupper() else raw_city, 'HU')
    )
    return venue, city, country_code


def archive_urls(session):
    urls = set()
    for page_number in range(1, 101):
        url = (
            CONCERT_ARCHIVE_URL if page_number == 1
            else urljoin(CONCERT_ARCHIVE_URL, f'page/{page_number}/')
        )
        response = session.get(url, timeout=45)
        if response.status_code == 404 and page_number > 1:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        page_urls = {
            urljoin(SOURCE_URL, anchor['href'])
            for article in soup.select('article.post')
            for anchor in article.select('a[href]')
            if urlparse(urljoin(SOURCE_URL, anchor['href'])).netloc
            == urlparse(SOURCE_URL).netloc
        }
        if not page_urls or page_urls.issubset(urls):
            break
        urls.update(page_urls)
    return sorted(urls)


def labelled_value(soup, label):
    for box in soup.select('.elementor-icon-box-wrapper'):
        title = clean_text(box.select_one('.elementor-icon-box-title')).casefold()
        if label in title:
            return clean_text(box.select_one('.elementor-icon-box-description'))
    return ''


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.find('title'))
    title = re.sub(r'\s+[–—-]\s+Capella Savaria\s*$', '', title, flags=re.I)
    parsed_datetime = parse_datetime(labelled_value(soup, 'dátum'))
    location = parse_location(labelled_value(soup, 'helyszín'))
    if not title or not parsed_datetime or not location:
        return None

    descriptions = []
    for node in soup.select(
        '[data-elementor-type="wp-post"] .elementor-widget-text-editor '
        '.elementor-widget-container'
    ):
        text = clean_text(node)
        if text and text not in descriptions:
            descriptions.append(text)

    venue, city, country_code = location
    return {
        'title': title,
        'date': parsed_datetime[0],
        'url': url,
        'time_from': parsed_datetime[1],
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(descriptions) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in archive_urls(session):
        try:
            record = parse_event(get_response(session, url).content, url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Capella Savaria concert',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped Capella Savaria page with incomplete event fields',
                event='crawler_item_skipped',
                level='warning',
                url=url,
            )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class CapellaSavariaHuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='capellasavaria_hu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    CapellaSavariaHuCrawler().run()


if __name__ == '__main__':
    main()
