import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.redlandssymphony.com/'
SOURCE = 'Redlands Symphony Orchestra'
CITY = 'Redlands'
COUNTRY_CODE = 'US'

LISTING_URLS = (
    urljoin(SOURCE_URL, '26-27-season'),
    urljoin(SOURCE_URL, '25-26-season'),
    urljoin(SOURCE_URL, 'recitals'),
    urljoin(SOURCE_URL, 'specialevents'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+20\d{2})\b', re.I
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP]M)\b', re.I)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    text = re.sub(r'\bNOVMBER\b', 'NOVEMBER', clean_text(text), flags=re.I)
    match = DATE_RE.search(text)
    if not match:
        return None
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', match.group(1), flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(clean_text(text))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0).replace(' ', ''), '%I:%M%p').strftime('%H:%M')
    except ValueError:
        try:
            return datetime.strptime(match.group(0).replace(' ', ''), '%I%p').strftime('%H:%M')
        except ValueError:
            return None


def make_record(title, event_date, url, time_from, venue, description):
    if not all((title, event_date, url, venue)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': description or None,
    }


def parse_season(soup, page_url):
    records = []
    for section in soup.select('section[id]'):
        title_node = section.select_one('h1')
        details_node = section.select_one('h3')
        if title_node is None or details_node is None:
            continue
        details = clean_text(details_node)
        event_date = parse_date(details)
        if not event_date:
            continue

        detail_link = section.find('a', href=True, string=re.compile(r'Learn More', re.I))
        url = urljoin(page_url, detail_link['href']) if detail_link else f'{page_url}#{section["id"]}'
        detail_lines = [line for line in details.splitlines() if line]
        venue = next(
            (line for line in detail_lines if not DATE_RE.search(line) and not TIME_RE.search(line)),
            'Clock Auditorium',
        )
        description = '\n\n'.join(
            clean_text(paragraph) for paragraph in section.select('p') if clean_text(paragraph)
        )
        record = make_record(
            clean_text(title_node), event_date, url, parse_time(details), venue, description
        )
        if record:
            records.append(record)
    return records


def parse_archived_season(soup, page_url):
    records = []
    for card in soup.select('p.paragraph-254'):
        container = card.parent
        title_node = container.select_one('h1')
        details = clean_text(card)
        event_date = parse_date(details)
        if title_node is None or not event_date:
            continue
        lines = [line for line in details.splitlines() if line]
        venue = next(
            (line.split('|', 1)[1].strip() for line in lines if '|' in line), None
        )
        link = container.find('a', href=True, string=re.compile(r'Learn More', re.I))
        url = urljoin(page_url, link['href']) if link else page_url
        description = '\n'.join(line for line in lines if not DATE_RE.search(line))
        record = make_record(
            clean_text(title_node).replace('\n', ' '), event_date, url,
            parse_time(details), venue, description,
        )
        if record:
            records.append(record)
    return records


def parse_recitals(soup, page_url):
    records = []
    for card in soup.select('.w-col'):
        title_node = card.select_one('h2')
        details_node = card.select_one('p')
        if title_node is None or details_node is None:
            continue
        details = clean_text(details_node)
        event_date = parse_date(details)
        if not event_date:
            continue
        lines = [line for line in details.splitlines() if line]
        venue = next(
            (line for line in lines if not DATE_RE.search(line) and not TIME_RE.search(line)), None
        )
        link = card.find('a', href=True, string=re.compile(r'Tickets', re.I))
        url = urljoin(page_url, link['href']) if link else page_url
        subtitle = clean_text(card.select_one('h4'))
        record = make_record(
            clean_text(title_node), event_date, url, parse_time(details), venue, subtitle
        )
        if record:
            records.append(record)
    return records


def parse_special_events(soup, page_url):
    records = []
    for card in soup.select('.uui-blog05_item-2'):
        title_node = card.select_one('h3')
        details_node = card.select_one('.uui-text-size-medium-13')
        if title_node is None or details_node is None:
            continue
        details = clean_text(details_node)
        event_date = parse_date(details)
        if not event_date:
            continue
        lines = [line for line in details.splitlines() if line]
        venue = next(
            (line for line in reversed(lines) if not DATE_RE.search(line) and not TIME_RE.search(line)),
            None,
        )
        link = card.find('a', href=True, string=re.compile(r'Tickets', re.I))
        url = urljoin(page_url, link['href']) if link else page_url
        record = make_record(
            clean_text(title_node), event_date, url, parse_time(details), venue, details
        )
        if record:
            records.append(record)
    return records


class RedlandsSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='redlandssymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        parsers = (parse_season, parse_archived_season, parse_recitals, parse_special_events)
        for url, parser in zip(LISTING_URLS, parsers):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Redlands Symphony events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            records.extend(parser(BeautifulSoup(response.text, 'html.parser'), url))

        if not records:
            log_message(
                'No Redlands Symphony concerts found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    RedlandsSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
