import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://folkoperan.se/'
CALENDAR_URL = urljoin(SOURCE_URL, 'pa-scen/')
SOURCE = 'Folkoperan'
VENUE = 'Folkoperan'
CITY = 'Stockholm'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1, 'februari': 2, 'mars': 3, 'april': 4,
    'maj': 5, 'juni': 6, 'juli': 7, 'augusti': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_swedish_date(value):
    match = re.search(
        r'(\d{1,2})\s+'
        r'(januari|februari|mars|april|maj|juni|juli|augusti|september|oktober|november|december)'
        r'\s+(\d{4})',
        clean_text(value).casefold(),
    )
    if not match:
        return None
    day, month_name, year = match.groups()
    try:
        return datetime(int(year), MONTHS[month_name], int(day)).date().isoformat()
    except ValueError:
        return None


def production_context(item):
    """Return the enclosing production link and its on-page introduction."""
    container = item.find_parent('div', class_=lambda value: value and 'c-body' in value)
    if not container:
        return None, None
    link = container.select_one('a[href*="/uppsattningar/"]')
    description_parts = []
    lead = container.select_one('.c-lead-paragraph')
    if lead:
        description_parts.append(clean_text(lead))
    return (urljoin(SOURCE_URL, link.get('href')) if link else None,
            '\n\n'.join(description_parts) or None)


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return None
    for node in main.select(
        '.c-event-list, nav, script, style, .c-cta, .c-production-meta, '
        '.c-social-share, .c-breadcrumbs'
    ):
        node.decompose()
    return clean_text(main) or None


class FolkoperanSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='folkoperan_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
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
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        raw_records = []
        detail_urls = set()
        for item in soup.select('.c-event-list__item'):
            title = clean_text(item.select_one('h3'))
            date_value = parse_swedish_date(item.select_one('h4'))
            ticket = item.select_one('a[href*="biljetter.folkoperan.se/"]')
            time_node = item.select_one('.u-small-text')
            time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', clean_text(time_node))
            detail_url, summary = production_context(item)
            url = urljoin(SOURCE_URL, ticket.get('href')) if ticket else detail_url
            if not all((title, date_value, url)):
                continue
            if detail_url:
                detail_urls.add(detail_url)
            raw_records.append({
                'title': title,
                'date': date_value,
                'url': url,
                'time_from': time_match.group(0) if time_match else None,
                'venue': VENUE,
                'city': CITY,
                'country_code': 'SE',
                'description': summary,
                'source_url': SOURCE_URL,
                'source': SOURCE,
                '_detail_url': detail_url,
            })

        descriptions = {}
        for url in detail_urls:
            try:
                descriptions[url] = detail_description(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Folkoperan production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        unique_records = {}
        for record in raw_records:
            detail_url = record.pop('_detail_url')
            record['description'] = descriptions.get(detail_url) or record['description']
            key = (record['title'], record['date'], record['time_from'], record['venue'])
            unique_records.setdefault(key, record)
        return sorted(unique_records.values(), key=lambda item: (
            item['date'], item['time_from'] or '', item['title']
        ))


def main():
    FolkoperanSeCrawler().run()


if __name__ == '__main__':
    main()
