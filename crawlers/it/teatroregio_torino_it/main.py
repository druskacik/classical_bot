import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroregio.torino.it/'
SOURCE = 'Teatro Regio Torino'
FIRST_API_YEAR = 2018

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# These first-party calendar classifications contain concrete performances.
# Talks and participatory activities have separate classifications and are omitted.
PERFORMANCE_PARENTS = {
    'Opera e balletto',
    'Concerti',
    'Concerti extra',
    'In famiglia',
}
PERFORMANCE_CATEGORIES = {
    'Spettacoli',
    'Regio Opera Festival',
    'Musica a Regio aperto',
    "Passaggi d'estate",
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_item(item):
    content = BeautifulSoup(item.get('content', ''), 'html.parser')
    link = content.select_one('a[href]:not(.calendar-activity-tickets)')
    if link is None:
        return None
    title = clean_text(link)
    href = link.get('href', '').strip()
    time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', content.get_text(' ', strip=True))
    try:
        event_date = datetime.strptime(item['date'], '%m-%d-%Y').date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    if not title or not href:
        return None
    return title, event_date, urljoin(SOURCE_URL, href), time_match.group(0) if time_match else None


def is_performance(item):
    category = clean_text(item.get('category')).casefold()
    parent = BeautifulSoup(str(item.get('parent_target_id', '')), 'html.parser').get_text().strip().casefold()
    return (
        parent in {value.casefold() for value in PERFORMANCE_PARENTS}
        or category in {value.casefold() for value in PERFORMANCE_CATEGORIES}
    )


def detail_fields(soup):
    location = soup.select_one('.calendar-location')
    venue = clean_text(location)
    venue = re.sub(r'^Luogo di svolgimento:\s*', '', venue, flags=re.I).strip()
    if not venue:
        return None

    article = soup.select_one('article')
    if article is None:
        description = None
    else:
        article = BeautifulSoup(str(article), 'html.parser')
        for node in article.select(
            '.calendar-location, .calendar-activity-tickets, '
            '.view-calendar-activity, script, style, nav'
        ):
            node.decompose()
        description = clean_text(article) or None
    return venue, description


class TeatroregioTorinoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroregio_torino_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = []
        for year in range(FIRST_API_YEAR, date.today().year + 3):
            api_url = urljoin(SOURCE_URL, f'data/c/{year}?_format=json')
            try:
                response = session.get(api_url, timeout=45)
                response.raise_for_status()
                year_items = response.json()
                if not isinstance(year_items, list):
                    raise ValueError('calendar API response is not a list')
                items.extend(item for item in year_items if is_performance(item))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Teatro Regio calendar year',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=api_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        details = {}
        records = []
        for item in items:
            parsed = calendar_item(item)
            if parsed is None:
                continue
            title, event_date, url, time_from = parsed
            if url not in details:
                try:
                    response = session.get(url, timeout=45)
                    response.raise_for_status()
                    details[url] = detail_fields(BeautifulSoup(response.content, 'html.parser'))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Teatro Regio event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    details[url] = None
            if details[url] is None:
                continue
            venue, description = details[url]
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': 'Torino',
                'country_code': 'IT',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    TeatroregioTorinoItCrawler().run()


if __name__ == '__main__':
    main()
