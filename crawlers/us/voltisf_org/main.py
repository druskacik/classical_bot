import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.voltisf.org/'
SOURCE = 'Volti'
PAGES = [SOURCE_URL, f'{SOURCE_URL}tickets']
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>20\d{2}))?'
    r'(?:\s*[-–,]?\s*(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_date(month, day, year):
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = re.sub(r'\.', '', clean_text(value)).upper()
    value = re.sub(r'(?<=\d)(AM|PM)$', r' \1', value)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def block_lines(block):
    return [clean_text(line) for line in block.get_text('\n', strip=True).splitlines() if clean_text(line)]


def title_from_block(block):
    headings = [clean_text(node.get_text(' ', strip=True)) for node in block.select('h1, h2, h3, h4')]
    headings = [heading for heading in headings if heading]
    return ': '.join(headings) if headings else ''


def event_location(lines, last_match):
    remainder = last_match.string[last_match.end():]
    candidates = [clean_text(line) for line in remainder.split('\n') if clean_text(line)]
    if not candidates:
        candidates = lines[len(list(DATE_RE.finditer('\n'.join(lines)))):]

    venue = next((line for line in candidates if not re.search(r'\d', line)), '')
    address = next((line for line in candidates if re.search(r'\d', line) and ',' in line), '')
    city = clean_text(address.rsplit(',', 1)[-1]) if address else ''
    return venue, city


def parse_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = soup.select('main .sqs-block')
    records = []

    for index, block in enumerate(blocks):
        text = block.get_text('\n', strip=True)
        matches = list(DATE_RE.finditer(text))
        if not matches:
            continue

        title_index = None
        title = ''
        for previous in range(index - 1, max(-1, index - 7), -1):
            candidate = title_from_block(blocks[previous])
            if candidate and not re.search(r'\b20\d{2}\b', candidate):
                title_index = previous
                title = candidate
                break
        if not title:
            continue

        context = ' '.join(item.get_text(' ', strip=True) for item in blocks[max(0, index - 8):index + 1])
        context_year = re.search(r'\b(20\d{2})\b', context)
        lines = block_lines(block)
        venue, city = event_location(lines, matches[-1])
        if not venue or not city:
            continue

        detail_blocks = blocks[title_index:index] if title_index is not None else []
        detail_parts = []
        event_url = page_url
        for detail_block in detail_blocks:
            if DATE_RE.search(detail_block.get_text(' ', strip=True)):
                continue
            detail_text = clean_text(detail_block.get_text(' ', strip=True))
            if detail_text and detail_text != title and detail_text.lower() not in {'buy tickets', 'get tickets'}:
                detail_parts.append(detail_text)
            for link in detail_block.select('a[href]'):
                href = link.get('href', '')
                if event_url == page_url and href.startswith('http') and 'voltisf.org' not in href:
                    event_url = href
                    break

        description = '\n\n'.join(dict.fromkeys(detail_parts)) or None
        for match in matches:
            year = match.group('year') or (context_year.group(1) if context_year else '')
            event_date = parse_date(match.group('month'), match.group('day'), year)
            if not event_date:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': event_url,
                'time_from': parse_time(match.group('time')) if match.group('time') else None,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page_url in PAGES:
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            page_records = parse_page(response.text, page_url)
            records.extend(page_records)
            log_message(
                'Volti page scraped',
                event='crawler_page_scraped',
                url=page_url,
                record_count=len(page_records),
            )
        except requests.RequestException as error:
            log_message(
                'Volti page request failed',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class VoltisfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='voltisf_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    VoltisfOrgCrawler().run()


if __name__ == '__main__':
    main()
