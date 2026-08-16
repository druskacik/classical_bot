import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://osny.org/'
SOURCE = 'Oratorio Society of New York'
API_URL = 'https://osny.org/wp-json/wp/v2/concert'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+at\s+'
    r'(\d{1,2}:\d{2}\s*[ap]m)$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.match(clean_text(value))
    if not match:
        return None
    try:
        starts_at = datetime.strptime(
            f'{match.group(1)} {match.group(2).replace(" ", "")}',
            '%B %d, %Y %I:%M%p',
        )
    except ValueError:
        return None
    return starts_at.date().isoformat(), starts_at.strftime('%H:%M')


def parse_event_page(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    title_node = soup.select_one('.et_pb_section_0_tb_body h1') or soup.find('h1')
    title = clean_text(title_node)
    section = title_node.find_parent(class_=re.compile(r'et_pb_section_\d+_tb_body')) if title_node else None
    if not title or not section:
        return None

    modules = section.select('div.et_pb_text')
    date_index = None
    parsed_start = None
    for index, module in enumerate(modules):
        parsed_start = parse_date_time(module.get_text(' ', strip=True))
        if parsed_start:
            date_index = index
            break
    if date_index is None or date_index + 1 >= len(modules):
        return None

    venue = clean_text(modules[date_index + 1]).replace('\n', ' ')
    if not venue or parse_date_time(venue):
        return None

    description_parts = []
    for module in modules[date_index + 2:]:
        text = clean_text(module)
        if not text:
            continue
        if text.casefold() == 'other season concerts':
            break
        if text.lower().startswith(('single tickets', 'concert duration:')):
            continue
        if text not in description_parts:
            description_parts.append(text)

    date, time_from = parsed_start
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'New York',
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OsnyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osny_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        try:
            while True:
                response = session.get(
                    API_URL,
                    params={'per_page': 100, 'page': page},
                    timeout=45,
                )
                response.raise_for_status()
                items = response.json()
                if not isinstance(items, list):
                    raise ValueError('OSNY concert API returned an unexpected response')

                for item in items:
                    url = item.get('link')
                    if not url:
                        continue
                    detail_response = session.get(url, timeout=45)
                    detail_response.raise_for_status()
                    record = parse_event_page(detail_response.text, url)
                    if record:
                        records.append(record)

                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                if page >= total_pages:
                    break
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch OSNY concerts',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not records:
            log_message(
                'No parseable OSNY concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=API_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OsnyOrgCrawler().run()


if __name__ == '__main__':
    main()
