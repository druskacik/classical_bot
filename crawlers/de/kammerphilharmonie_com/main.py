import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kammerphilharmonie.com/'
SOURCE = 'Die Deutsche Kammerphilharmonie Bremen'
CALENDAR_URL = urljoin(SOURCE_URL, 'en/experience/concert-calendar/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}

COUNTRIES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'canada': 'CA',
    'china': 'CN', 'croatia': 'HR', 'czech republic': 'CZ', 'czechia': 'CZ',
    'denmark': 'DK', 'estonia': 'EE', 'finland': 'FI', 'france': 'FR',
    'germany': 'DE', 'greece': 'GR', 'hungary': 'HU', 'iceland': 'IS',
    'ireland': 'IE', 'italy': 'IT', 'japan': 'JP', 'latvia': 'LV',
    'lithuania': 'LT', 'luxembourg': 'LU', 'netherlands': 'NL',
    'norway': 'NO', 'poland': 'PL', 'portugal': 'PT', 'romania': 'RO',
    'singapore': 'SG', 'slovakia': 'SK', 'slovenia': 'SI', 'south korea': 'KR',
    'spain': 'ES', 'españa': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'taiwan': 'TW', 'turkey': 'TR', 'united kingdom': 'GB', 'great britain': 'GB',
    'usa': 'US', 'united states': 'US',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = re.search(r'(\d{1,2})[.:](\d{2})\s*([ap])\.?m\.?', value, re.I)
    if not match:
        match = re.search(r'\b([01]?\d|2[0-3])[.:](\d{2})\b', value)
        return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_location(element):
    parts = [
        re.sub(r'\s+', ' ', value).strip()
        for value in element.stripped_strings
        if value.strip() and value.strip() != '·'
    ]
    if len(parts) < 2:
        return None

    country_code = COUNTRIES.get(parts[0].casefold())
    if country_code:
        if len(parts) < 3:
            return None
        city = parts[1]
        venue = ', '.join(parts[2:])
    else:
        country_code = 'DE'
        city = parts[0]
        venue = ', '.join(parts[1:])
    if not city or not venue:
        return None
    return city, venue, country_code


def parse_card(card, year):
    link = card.select_one('h2 a[href]')
    title = clean_text(link)
    date_text = clean_text(card.select_one('.meta-date'))
    match = re.search(r'(\d{1,2})\.(\d{1,2})\.', date_text)
    location = parse_location(card.select_one('.meta-place')) if card.select_one('.meta-place') else None
    if not link or not title or not match or not location:
        return None
    try:
        event_date = date(year, int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None

    city, venue, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(CALENDAR_URL, link['href']),
        'time_from': parse_time(clean_text(card.select_one('.meta-time'))),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    sections = []
    for heading in soup.select('main h2'):
        if clean_text(heading).casefold() != 'programme':
            continue
        container = heading.find_parent(class_='item') or heading.parent
        text = clean_text(container)
        if text:
            sections.append(text)
    return '\n\n'.join(dict.fromkeys(sections)) or None


class KammerphilharmonieComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kammerphilharmonie_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def _get(self, session, url):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Kammerphilharmonie page',
                event='crawler_fetch_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        landing = BeautifulSoup(self._get(session, CALENDAR_URL).text, 'html.parser')
        years = sorted({
            int(node['value']) for node in landing.select('input[name="year"][value]')
            if node['value'].isdigit()
        })
        if not years:
            raise ValueError('Could not discover calendar years')

        records = []
        for year in years:
            first_url = f'{CALENDAR_URL}year:{year}/month:all/'
            first_soup = BeautifulSoup(self._get(session, first_url).text, 'html.parser')
            pages = {1}
            for link in first_soup.select('a[href*="/page/"]'):
                match = re.search(r'/page/(\d+)/', link.get('href', ''))
                if match:
                    pages.add(int(match.group(1)))
            for page in range(1, max(pages) + 1):
                soup = first_soup if page == 1 else BeautifulSoup(
                    self._get(session, f'{first_url}page/{page}/').text, 'html.parser'
                )
                for card in soup.select('article.event-card'):
                    record = parse_card(card, year)
                    if record:
                        records.append(record)

        unique_urls = sorted({record['url'] for record in records})
        descriptions = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._get, session, url): url for url in unique_urls}
            for future in as_completed(futures):
                url = futures[future]
                descriptions[url] = parse_description(future.result().text)
        for record in records:
            record['description'] = descriptions.get(record['url'])

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['city'], item['title'], item['url']
        ))


def main():
    KammerphilharmonieComCrawler().run()


if __name__ == '__main__':
    main()
