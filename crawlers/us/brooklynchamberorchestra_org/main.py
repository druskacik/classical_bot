import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://brooklynchamberorchestra.org/'
SOURCE = 'Brooklyn Chamber Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concerts'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/html;q=0.9,*/*;q=0.8',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    text = clean_text(value).replace(',', '')
    for pattern in ('%d %B %Y', '%B %d %Y'):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_time(value):
    text = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p', '%H:%M'):
        try:
            return datetime.strptime(text, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def meta_value(container, label):
    wanted = label.casefold()
    for block in container.select('.concert-meta-block'):
        heading = block.select_one('.title')
        if clean_text(heading).casefold() == wanted:
            return clean_text(block.select_one('h3'))
    return ''


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    details = soup.select_one('.concert-details')
    if not details:
        return None

    title = clean_text(details.select_one('h1'))
    date = parse_date(meta_value(details, 'Date'))
    time_from = parse_time(meta_value(details, 'Time'))
    venue = clean_text(meta_value(details, 'Venue'))
    city_label = clean_text(meta_value(details, 'City'))
    city = city_label.split(',', 1)[0].strip()

    description = None
    content_blocks = details.select('.content-block')
    if len(content_blocks) > 1:
        description_block = content_blocks[1]
        section_title = description_block.select_one('.title')
        if section_title:
            section_title.decompose()
        description = clean_text(description_block) or None

    if not all((title, date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BrooklynChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brooklynchamberorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            response = session.get(
                API_URL,
                params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'asc'},
                timeout=45,
            )
            response.raise_for_status()
            events = response.json()
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))

            for event in events:
                url = clean_text(event.get('link'))
                if not url:
                    continue
                try:
                    detail_response = session.get(url, timeout=45)
                    detail_response.raise_for_status()
                    record = parse_event(detail_response.text, url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Brooklyn Chamber Orchestra event',
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
                        'Skipped incomplete Brooklyn Chamber Orchestra event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, venue, or city is missing',
                    )

            if page >= total_pages:
                break
            page += 1

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    BrooklynChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
