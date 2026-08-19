import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.muse-ique.com/'
SOURCE = 'MUSE/IQUE'
CALENDAR_URL = urljoin(SOURCE_URL, 'events-and-rsvp')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month: number for number, month in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        1,
    )
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_links(soup):
    links = set()
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        parsed = urlparse(url)
        path = parsed.path.rstrip('/')
        if parsed.netloc == 'www.muse-ique.com' and path.startswith('/events-and-rsvp/'):
            links.add(f'{parsed.scheme}://{parsed.netloc}{path}')
    return sorted(links)


def season_year(text):
    match = re.search(r"MUSE/IQUE(?:'S|’S)\s+(20\d{2})\s+SEASON", text, re.I)
    return int(match.group(1)) if match else None


def venue_cities(text):
    cities = {}
    venue_section = text.split('\nVENUES\n', 1)
    if len(venue_section) == 1:
        return cities
    lines = [line.strip() for line in venue_section[1].splitlines() if line.strip()]
    for index, line in enumerate(lines[:-2]):
        if re.match(r'^\d', lines[index + 1]):
            city_match = re.match(r'^(.+?),\s*CA\s+\d{5}', lines[index + 2], re.I)
            if city_match:
                cities[line.casefold()] = (line, city_match.group(1).strip())
    return cities


def parse_detail(soup, url, year):
    text = clean_text(soup)
    title = clean_text(soup.title)
    title = re.sub(r'\s*\|\s*MUSE/IQUE\s*$', '', title, flags=re.I).strip()
    if not title or not year or '\nSHOWTIMES\n' not in text:
        return []

    schedule = text.split('\nSHOWTIMES\n', 1)[1]
    schedule = schedule.split('\nABOUT ', 1)[0]
    lines = [line.strip() for line in schedule.splitlines() if line.strip()]
    locations = venue_cities(text)
    records = []

    for index, line in enumerate(lines):
        match = re.match(
            r'^(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{1,2})\s+-\s+(.+)$',
            line,
            re.I,
        )
        if not match:
            continue

        venue_line = next((item[1:].strip() for item in lines[index + 1:] if item.startswith('@')), '')
        location = locations.get(venue_line.casefold())
        if not location:
            continue

        month_name = match.group(1).title()
        try:
            event_date = date(year, MONTHS[month_name], int(match.group(2))).isoformat()
        except ValueError:
            continue

        times = re.findall(r'\b(?:1[0-2]|0?[1-9]):[0-5]\d\s*(?:am|pm)\b', match.group(3), re.I)
        for value in times:
            parsed_time = datetime.strptime(value.upper(), '%I:%M %p')
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parsed_time.strftime('%H:%M'),
                'venue': location[0],
                'city': location[1],
                'country_code': 'US',
                'description': text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class MuseIqueComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='muse_ique_com',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
            calendar_soup = BeautifulSoup(response.text, 'html.parser')
            year = season_year(clean_text(calendar_soup))
            if not year:
                raise ValueError('Could not determine the calendar season year')

            records = []
            for url in event_links(calendar_soup):
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                records.extend(parse_detail(BeautifulSoup(detail_response.text, 'html.parser'), url, year))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape MUSE/IQUE calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['venue'], record['title']
        ))


def main():
    MuseIqueComCrawler().run()


if __name__ == '__main__':
    main()
