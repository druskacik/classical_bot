import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.irishchamberorchestra.com/'
SOURCE = 'Irish Chamber Orchestra'
EVENTS_URL = urljoin(SOURCE_URL, 'whats-on/concerts-and-events')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-IE,en;q=0.9',
}
TIME_RE = re.compile(r'\bat\s+(\d{1,2}(?:[.:]\d{1,2})?\s*(?:am|pm)?)\b', re.I)
DATE_RE = re.compile(r'\b(\d{1,2}\s+[A-Za-z]+)(?:\s+(\d{4}))?\b')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(text):
    match = TIME_RE.search(clean_text(text))
    if not match:
        return None
    value = match.group(1).replace('.', ':').replace(' ', '').lower()
    if re.fullmatch(r'\d{1,2}', value):
        # The calendar occasionally abbreviates an evening time as "at 8".
        hour = int(value)
        return f'{hour + 12 if 1 <= hour <= 11 else hour:02d}:00'
    if ':' not in value:
        value = re.sub(r'(?=am$|pm$)', ':00', value)
    for pattern in ('%I:%M%p', '%H:%M'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_date(text, year):
    match = DATE_RE.search(clean_text(text))
    if not match:
        return ''
    date_year = match.group(2) or year
    if not date_year:
        return ''
    try:
        return datetime.strptime(f'{match.group(1)} {date_year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return ''


def header_cities(soup):
    header_values = [clean_text(node) for node in soup.select('.h5.regular-font.mb-0')]
    if not header_values:
        return []
    return [city.strip() for city in header_values[0].split(',') if city.strip()]


def occurrence_city(venue, cities, index):
    venue_folded = venue.casefold()
    # The first-party filter/header uses the county for this occurrence, while
    # the venue string identifies the actual town.
    if re.search(r'\bnewport\s+co\.?\s*mayo\b', venue_folded):
        return 'Newport'
    for city in cities:
        if re.search(rf'\b{re.escape(city.casefold())}\b', venue_folded):
            return city
    if len(cities) == 1:
        return cities[0]
    if index < len(cities):
        return cities[index]
    return ''


def venue_name(value, city):
    venue = clean_text(value)
    venue = re.sub(r'\s+\d{1,2}[.:]\d{2}\s*(?:am|pm)?\s*$', '', venue, flags=re.I)
    if city:
        venue = re.sub(rf',\s*{re.escape(city)}\s*$', '', venue, flags=re.I)
    return venue.strip(' ,')


def parse_detail(soup, url):
    title_node = soup.select_one('h1')
    title = clean_text(title_node)
    cities = header_cities(soup)
    header_text = ' '.join(clean_text(node) for node in soup.select('.h5.regular-font.mb-0'))
    year_match = re.search(r'\b(20\d{2})\b', header_text)
    year = year_match.group(1) if year_match else ''
    description_parts = []
    subtitle = clean_text(soup.select_one('h1 + h2'))
    if subtitle:
        description_parts.append(subtitle)
    for block in soup.select('.content-block .text-wrapper'):
        text = clean_text(block)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for index, article in enumerate(soup.select('#tickets article')):
        date_time_node = article.select_one('.fw-bold')
        venue_node = article.select_one('.venue')
        date_time = clean_text(date_time_node)
        displayed_venue = clean_text(venue_node)
        event_date = parse_date(date_time, year)
        city = occurrence_city(displayed_venue, cities, index)
        venue = venue_name(displayed_venue, city)
        if not all((title, event_date, venue, city)):
            log_message(
                'Skipped incomplete Irish Chamber Orchestra occurrence',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
                error_message='Required title, date, venue, or city is missing',
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_time),
            'venue': venue,
            'city': city,
            'country_code': 'IE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class IrishChamberOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='irishchamberorchestra_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(EVENTS_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = list(dict.fromkeys(
            urljoin(EVENTS_URL, link['href'])
            for link in soup.select('a.link-block[href*="/concerts-and-events/"]')
        ))

        records = []
        for url in urls:
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                records.extend(parse_detail(BeautifulSoup(detail_response.text, 'html.parser'), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Irish Chamber Orchestra event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    IrishChamberOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
