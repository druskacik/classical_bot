import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.stavanger-konserthus.no/'
SOURCE = 'Stavanger Konserthus'
SITEMAP_URL = SOURCE_URL + 'event-sitemap.xml'
HEADERS = {
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    match = re.search(r'(\d{1,2})\.\s+([A-Za-zÆØÅæøå]+)\s+(\d{4})', value)
    months = {
        'januar': 1, 'jan': 1, 'februar': 2, 'feb': 2,
        'mars': 3, 'mar': 3, 'april': 4, 'apr': 4,
        'mai': 5, 'juni': 6, 'jun': 6, 'juli': 7, 'jul': 7,
        'august': 8, 'aug': 8, 'september': 9, 'sep': 9,
        'oktober': 10, 'okt': 10, 'november': 11, 'nov': 11,
        'desember': 12, 'des': 12,
    }
    if not match:
        return None
    try:
        return datetime(
            int(match.group(3)), months[match.group(2).casefold()], int(match.group(1))
        ).date().isoformat()
    except (KeyError, ValueError):
        return None


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return list(dict.fromkeys(
        clean_text(node.get_text())
        for node in soup.find_all('loc')
        if '/event/' in node.get_text()
    ))


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = soup.select_one('#event')
    title_node = event.select_one('h1') if event else None
    row = event.select_one('#shows tr:not(.sales-status)') if event else None
    cells = row.select('td') if row else []
    date_node = row.select_one('.uk-visible-medium') if row else None
    date_value = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
    time_value = next(
        (clean_text(cell.get_text(' ', strip=True)) for cell in cells if re.fullmatch(r'\d{1,2}:\d{2}', clean_text(cell.get_text(' ', strip=True)))),
        None,
    )
    venue_heading = event.select_one('h2') if event else None
    venue_node = venue_heading.select_one('strong') if venue_heading else None
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')

    if not title or not date_value or not venue:
        return None

    description_node = event.select_one('.event-content')
    description = clean_text(description_node.get_text('\n', strip=True)) if description_node else None
    return {
        'title': title,
        'date': date_value,
        'url': url,
        'time_from': time_value,
        'venue': venue,
        'city': 'Stavanger',
        'description': description or None,
    }


class StavangerKonserthusNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stavanger_konserthus_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _get(self, url):
        response = requests.get(
            url,
            headers=HEADERS,
            impersonate='chrome',
            timeout=45,
        )
        response.raise_for_status()
        return response.text

    def _scrape_event(self, url):
        try:
            record = parse_event(self._get(url), url)
            if record is None:
                log_message(
                    'Skipping incomplete Stavanger Konserthus event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEvent',
                    error_message='Missing title, date, or venue',
                )
            return record
        except Exception as error:
            log_message(
                'Failed to fetch Stavanger Konserthus event',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        urls = event_urls(self._get(SITEMAP_URL))
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._scrape_event, url): url for url in urls}
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    return StavangerKonserthusNoCrawler().run()


if __name__ == '__main__':
    main()
