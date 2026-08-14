from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oslokonserthus.no/'
SOURCE = 'Oslo Konserthus'
PROGRAM_API = 'https://oslokonserthus.no/api/program'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}


def clean_text(node):
    if node is None:
        return None
    text = node.get_text('\n', strip=True)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    value = '\n'.join(line for line in lines if line).strip()
    return value or None


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('main article .innholdstekst'))


def occurrence_record(event, occurrence, description):
    title = str(event.get('title') or '').strip()
    url = str(event.get('url') or '').strip()
    venue = str((event.get('sal') or {}).get('label') or '').strip()
    start_value = occurrence.get('start')
    if not title or not url or not venue or not start_value:
        return None
    try:
        start = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': 'Oslo',
        'description': description,
    }


class OsloKonserthusNoCrawler(BaseCrawler):
    # Oslo Konserthus is a mixed venue. The Klassisk category omits eligible
    # film concerts, crossover events, family performances, and musicals. The
    # complete first-party program is therefore sent through classification.
    config = CrawlerConfig(
        slug='oslokonserthus_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        response = requests.get(
            PROGRAM_API,
            params={'kategori': 'all', 'sal': '', 'serie': ''},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list):
            raise ValueError('Oslo Konserthus program API did not return a list')

        descriptions = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(fetch_description, event['url']): event['url']
                for event in events
                if event.get('url')
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except (requests.RequestException, ValueError) as error:
                    descriptions[url] = None
                    log_message(
                        'Failed to fetch Oslo Konserthus event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for event in events:
            description = descriptions.get(event.get('url'))
            for occurrence in event.get('alledatoer') or []:
                record = occurrence_record(event, occurrence, description)
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'], item['title'], item['venue']
        ))


def main():
    return OsloKonserthusNoCrawler().run()


if __name__ == '__main__':
    main()
