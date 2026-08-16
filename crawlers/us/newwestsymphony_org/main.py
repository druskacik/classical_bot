import calendar
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://newwestsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
SOURCE = 'New West Symphony'
ZIP_CITIES = {
    '91360': 'Thousand Oaks',
    '91361': 'Thousand Oaks',
    '93065': 'Simi Valley',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<month>[A-Z][a-z]{2})\.\s*(?P<day>\d{1,2})\s*\|\s*'
    r'(?P<weekday>[A-Z][a-z]+)(?:\s*@\s*(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def infer_year(date_headings, page_text):
    """Infer the shared calendar year, verifying it against printed weekdays."""
    matches = [DATE_RE.search(value) for value in date_headings]
    matches = [match for match in matches if match]
    mentioned = [int(value) for value in re.findall(r'\b20\d{2}\b', page_text)]
    candidates = sorted(set(mentioned + list(range(datetime.now().year - 2, datetime.now().year + 4))))
    for year in candidates:
        if all(
            calendar.day_name[
                datetime.strptime(
                    f"{match.group('month')} {match.group('day')} {year}", '%b %d %Y'
                ).weekday()
            ].lower() == match.group('weekday').lower()
            for match in matches
        ):
            return year
    return None


def parse_time(value):
    if not value:
        return None
    for pattern in ('%I%p', '%I:%M%p'):
        try:
            return datetime.strptime(value.replace(' ', '').upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def location_from_heading(value):
    parts = [clean_text(part) for part in re.split(r'\s+[–—]\s+', value) if clean_text(part)]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return None, None


def location_from_card(card):
    paragraphs = [clean_text(node.get_text(' ', strip=True)) for node in card.select('p')]
    for value in paragraphs:
        zip_match = re.search(r'\bCA\s+(\d{5})\b', value)
        if zip_match and re.search(r'\s[–—]\s', value):
            venue, address = re.split(r'\s+[–—]\s+', value, maxsplit=1)
            known_city = ZIP_CITIES.get(zip_match.group(1))
            if known_city:
                return clean_text(venue), known_city
            city_match = re.search(r',?\s*([A-Za-z][A-Za-z .]+),\s*CA\s+\d{5}\b', address)
            if city_match:
                return clean_text(venue), clean_text(city_match.group(1))
    return None, None


def detail_description(session, url):
    if url == EVENTS_URL or urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
        return None
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        article = soup.select_one('article')
        return clean_text(article.get_text('\n', strip=True)) if article else None
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    cards = [row for row in soup.select('.et_pb_row') if row.select_one('h1') and row.select_one('h2')]
    headings = [clean_text(node.get_text(' ', strip=True)) for card in cards for node in card.select('h2')]
    year = infer_year(headings, clean_text(soup.get_text(' ', strip=True)))
    if year is None:
        log_message(
            'Could not infer calendar year',
            event='crawler_year_missing',
            level='warning',
            url=EVENTS_URL,
        )
        return []

    records = []
    for card in cards:
        title_node = card.select_one('h1')
        title = clean_text(title_node.get_text(' ', strip=True))
        links = card.select('a[href]')
        detail_link = next(
            (link for link in links if 'details' in clean_text(link.get_text(' ', strip=True)).lower()),
            None,
        )
        title_link = title_node.find('a', href=True)
        selected_link = detail_link or title_link
        url = urljoin(EVENTS_URL, selected_link['href']) if selected_link else EVENTS_URL
        card_description = clean_text(card.get_text('\n', strip=True))
        description = detail_description(session, url) or card_description or None
        card_venue, card_city = location_from_card(card)

        for heading_node in card.select('h2'):
            heading = clean_text(heading_node.get_text(' ', strip=True))
            match = DATE_RE.search(heading)
            if not match:
                continue
            try:
                event_date = datetime.strptime(
                    f"{match.group('month')} {match.group('day')} {year}", '%b %d %Y'
                ).date().isoformat()
            except ValueError:
                continue
            venue, city = location_from_heading(heading)
            venue, city = venue or card_venue, city or card_city
            if not title or not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(match.group('time')),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No valid concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class NewWestSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newwestsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    NewWestSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
