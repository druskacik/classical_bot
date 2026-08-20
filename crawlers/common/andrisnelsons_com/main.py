import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = 'Andris Nelsons'
SOURCE_URL = 'https://andrisnelsons.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule/')
PAST_SCHEDULE_URL = f'{SCHEDULE_URL}?type=past'

COUNTRY_CODES = {
    'Austria': 'AT',
    'China': 'CN',
    'France': 'FR',
    'Germany': 'DE',
    'Luxembourg': 'LU',
    'Switzerland': 'CH',
    'Taiwan': 'TW',
    'United Kingdom': 'GB',
    'United States': 'US',
}


def clean_text(value: str) -> str:
    return re.sub(r'\s+', ' ', value).strip(' ,\n\t')


def parse_schedule(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    records = []

    for item in soup.select('.schedule-item'):
        date_parts = [clean_text(span.get_text(' ', strip=True)) for span in item.select('.schedule-item-time > span')]
        title_node = item.select_one('.schedule-item-orchestras')
        venue_node = item.select_one('.schedule-item-venue')
        location_node = item.select_one('.schedule-item-state-country')

        if len(date_parts) != 3 or not title_node or not venue_node or not location_node:
            continue

        try:
            event_date = datetime.strptime(' '.join(date_parts), '%b %d %Y').date().isoformat()
        except ValueError:
            continue

        location_parts = [clean_text(part) for part in location_node.get_text(' ', strip=True).rsplit(',', 1)]
        if len(location_parts) != 2:
            continue
        city, country_name = location_parts
        country_code = COUNTRY_CODES.get(country_name)

        title = clean_text(title_node.get_text(' ', strip=True))
        venue = clean_text(venue_node.get_text(' ', strip=True))
        if not title or not venue or not city or not country_code:
            continue

        time_node = item.select_one('.schedule-item-time div')
        time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', time_node.get_text(' ', strip=True) if time_node else '')
        programme_node = item.select_one('.schedule-item-programm')
        description = programme_node.get_text('\n', strip=True) if programme_node else None
        ticket_node = item.select_one('a.schedule-item-tickets[href]')
        event_url = urljoin(page_url, ticket_node['href']) if ticket_node else page_url

        records.append({
            'title': title,
            'date': event_date,
            'url': event_url,
            'time_from': time_match.group(0) if time_match else None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
        })

    return records


class AndrisNelsonsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='andrisnelsons_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        front_fields=[
            ('source_url', SOURCE_URL),
            ('source', SOURCE),
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self) -> list[dict]:
        records = []
        session = requests.Session()
        session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; ClassicalBot/1.0)'

        for page_url in (SCHEDULE_URL, PAST_SCHEDULE_URL):
            log_message('Fetching schedule', event='crawler_url_fetch', url=page_url)
            response = session.get(page_url, timeout=30)
            response.raise_for_status()
            page_records = parse_schedule(response.text, page_url)
            log_message(
                'Schedule parsed',
                event='crawler_page_parsed',
                url=page_url,
                record_count=len(page_records),
            )
            records.extend(page_records)

        return records


def main():
    AndrisNelsonsCrawler().run()


if __name__ == '__main__':
    main()
