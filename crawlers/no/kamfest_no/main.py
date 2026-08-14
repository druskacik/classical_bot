import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kamfest.no/'
SOURCE = 'Trondheim kammermusikkfestival'
PROGRAM_URL = urljoin(SOURCE_URL, '/no/program/')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}
MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'des': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def festival_year(soup):
    banner = soup.find('header') or soup
    match = re.search(r'20\d{2}', clean_text(banner))
    return int(match.group()) if match else None


def parse_occurrences(soup, year):
    container = soup.select_one('.date-place .time-and-date')
    if not container or not year:
        return []
    occurrences = []
    for node in container.select('span'):
        text = clean_text(node).casefold().replace('.', '')
        match = re.search(
            r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s*-\s*'
            r'(\d{1,2})[.:](\d{2})',
            text,
        )
        if not match:
            continue
        try:
            event_date = date(year, MONTHS[match.group(2)], int(match.group(1)))
        except ValueError:
            continue
        hour, minute = int(match.group(3)), int(match.group(4))
        if hour > 23 or minute > 59:
            continue
        occurrences.append((event_date.isoformat(), f'{hour:02d}:{minute:02d}'))
    return list(dict.fromkeys(occurrences))


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('main article.main-event')
    title_node = event.select_one('h1') if event else None
    location_node = event.select_one('.date-place .location') if event else None
    venue = clean_text(location_node)
    title = clean_text(title_node)
    if not title or not venue:
        return []

    # This regional tour page lists different towns and venues for each time,
    # without associating them structurally. Returning its generic region as a
    # venue would create invalid records, so leave it for a future richer feed.
    if venue.casefold() in {'trøndelag', 'ulike arenaer'}:
        return []

    street_address = r'[^,]*(?:allé|gate|gata|veg|vegen|vei|veien)\s+\d+\b'
    address_only = re.fullmatch(street_address, venue, re.I)
    if address_only:
        venue = title
    elif re.search(r',\s*' + street_address + r'\s*$', venue, re.I):
        venue = venue.split(',', 1)[0].strip()

    occurrences = parse_occurrences(soup, festival_year(soup))
    if not occurrences:
        return []

    description_parts = []
    for selector in ('.assisting', '.event-content .body-text'):
        value = clean_text(event.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)
    description = '\n\n'.join(description_parts) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Trondheim',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


class KamfestNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kamfest_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(PROGRAM_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = list(dict.fromkeys(
            urljoin(PROGRAM_URL, link.get('href'))
            for link in soup.select('aside a[href^="/no/program/"]')
            if link.get('href') and link.get('href') != '/no/program/'
        ))

        records = []
        for url in urls:
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                records.extend(parse_event(detail.text, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Kamfest event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    return KamfestNoCrawler().run()


if __name__ == '__main__':
    main()
