import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://vivacissimofestival.it/'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-page-1.xml'
SOURCE = 'Vivacissimo Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,it;q=0.7',
}

MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def edition_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    urls = re.findall(r'<loc>(https://vivacissimofestival\.it/en/[^<]+)</loc>', response.text)
    editions = [
        url for url in urls
        if re.search(r'/(?:edition-20\d{2}|20\d{2}-edition)/?$', url)
    ]
    return sorted(set(editions))


def parse_year(soup, url):
    heading = soup.select_one('h1.elementor-heading-title')
    match = re.search(r'20\d{2}', clean_text(heading)) or re.search(r'20\d{2}', url)
    return int(match.group()) if match else None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*([AP]M)\b', value, re.I)
    if not match:
        return None
    hour, minute, period = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if period == 'PM' else 0)
    return f'{hour:02d}:{minute:02d}'


def parse_location(value):
    location = re.sub(r'^\s*📍\s*', '', clean_text(value)).replace('\n', ' ').strip()
    location = re.sub(r'\s+', ' ', location)
    if not location:
        return None

    parts = [part.strip() for part in location.rsplit(',', 1)]
    if len(parts) == 2 and parts[1]:
        venue, city = parts
    else:
        venue, city = location, 'Gambatesa'

    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue.title(), city.title()


def parse_page(soup, url):
    year = parse_year(soup, url)
    if year is None:
        return []

    records = []
    for day_node in soup.select('.data-giorno'):
        day_box = day_node.find_parent(class_='data-singola')
        day_match = re.search(r'\b(\d{1,2})\s*([A-Z]{3})\b', clean_text(day_node).upper())
        if day_box is None or not day_match:
            continue
        try:
            event_date = date(year, MONTHS[day_match.group(2)], int(day_match.group(1))).isoformat()
        except (KeyError, ValueError):
            continue

        for type_node in day_box.select('.data-ora'):
            if type_node.find_parent(class_='data-singola') is not day_box:
                continue
            type_text = clean_text(type_node)
            event_type = type_text.split('|', 1)[0].strip().casefold()
            if event_type != 'concert':
                continue

            event_box = type_node.parent
            title = clean_text(event_box.select_one('.data-nome'))
            location = parse_location(event_box.select_one('.data-luogo'))
            if not title or location is None:
                continue
            venue, city = location
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(type_text),
                'venue': venue,
                'city': city,
                'country_code': 'IT',
                'description': clean_text(event_box.select_one('.data-desc')) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class VivacissimoFestivalItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vivacissimofestival_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
            urls = edition_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Vivacissimo Festival sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_page(BeautifulSoup(response.content, 'html.parser'), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Vivacissimo Festival edition',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    VivacissimoFestivalItCrawler().run()


if __name__ == '__main__':
    main()
