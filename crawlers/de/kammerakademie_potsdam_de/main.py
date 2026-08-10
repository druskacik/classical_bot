import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kammerakademie-potsdam.de/'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Kammerakademie Potsdam'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

NON_GERMAN_CITIES = {
    'innsbruck': 'AT',
    'salzburg': 'AT',
    'wien': 'AT',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = {
        clean_text(location)
        for location in soup.find_all('loc')
        if '/event/' in clean_text(location)
    }
    archive_url = f'{SOURCE_URL}event/'
    return sorted(url for url in urls if url.rstrip('/') != archive_url.rstrip('/'))


def parse_date(value):
    try:
        parsed = datetime.strptime(value, '%d.%m.%Y').date()
    except (TypeError, ValueError):
        return None
    # The site has a small number of unfinished event pages containing the
    # WordPress epoch placeholder rather than an actual performance date.
    if parsed.year == 1970:
        return None
    return parsed.isoformat()


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})[.:](\d{2})(?:\s*Uhr)?', value or '', re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def description_from(soup):
    parts = []
    for element in soup.select('.content > .grid.mb_3 .text.content-box'):
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    visual = soup.select_one('.key-visual')
    title_element = visual.select_one('h1') if visual else None
    info = visual.select('p.text') if visual else []
    if not title_element or len(info) < 2:
        return None

    date_parts = [clean_text(part) for part in info[0].stripped_strings]
    location_parts = [clean_text(part) for part in info[1].stripped_strings]
    event_date = next((parse_date(part) for part in date_parts if parse_date(part)), None)
    time_from = next((parse_time(part) for part in date_parts if parse_time(part)), None)
    venue = location_parts[0] if location_parts else ''
    city = location_parts[1] if len(location_parts) > 1 else ''
    title = clean_text(title_element)

    if not title or not event_date or not venue or not city:
        return None

    country_code = NON_GERMAN_CITIES.get(city.casefold(), 'DE')
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class KammerakademiePotsdamDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kammerakademie_potsdam_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(SITEMAP_URL, timeout=45)
        response.raise_for_status()

        # The origin serves incomplete documents under concurrent load, so
        # details are deliberately fetched one at a time.
        records = []
        for url in event_urls(response.text):
            try:
                record = fetch_event(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Kammerakademie Potsdam event',
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
                    'Skipped incomplete Kammerakademie Potsdam event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    KammerakademiePotsdamDeCrawler().run()


if __name__ == '__main__':
    main()
