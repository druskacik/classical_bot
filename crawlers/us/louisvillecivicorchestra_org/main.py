import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.louisvillecivicorchestra.org/'
SOURCE = 'Louisville Civic Orchestra'
CITY = 'Louisville'
CONCERTS_URL = f'{SOURCE_URL}concerts'
ARCHIVE_URL = f'{SOURCE_URL}pastrepertoire'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|'
    r'Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\.?[,]?\s+'
    r'(?P<date>\d{1,2}/\d{1,2}/\d{2,4}|'
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|'
    r'Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4})\s*[,]?\s*'
    r'(?:@\s*)?(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)'
    r'\s*(?:@|\bat\b|-|\()\s*(?P<venue>.+)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', value, flags=re.I)
    value = value.replace(',', '').replace('.', '')
    for pattern in ('%m/%d/%y', '%m/%d/%Y', '%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    normalized = re.sub(r'\.', '', value).upper().replace(' ', '')
    if not normalized.endswith(('AM', 'PM')):
        hour = int(normalized.split(':', 1)[0])
        normalized += 'PM' if 1 <= hour <= 7 else 'AM'
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def clean_venue(value):
    venue = clean_text(value).replace('\n', ' ')
    venue = re.sub(r'\s*\*+\s*LOCATION CHANGED!*\s*\*+.*$', '', venue, flags=re.I)
    venue = re.sub(r'\s*\(Fun pre-concert activities.*$', '', venue, flags=re.I)
    if venue.endswith(')') and '(' not in venue:
        venue = venue[:-1]
    return venue.strip(' -')


def block_paragraphs(block):
    return [clean_text(node.get_text(' ', strip=True)) for node in block.find_all('p')]


def records_from_block(block, page_url):
    paragraphs = [value for value in block_paragraphs(block) if value]
    date_indexes = [index for index, value in enumerate(paragraphs) if DATE_RE.search(value)]
    if not date_indexes:
        return []

    groups = []
    for index in date_indexes:
        if groups and index == groups[-1][-1] + 1:
            groups[-1].append(index)
        else:
            groups.append([index])

    records = []
    for group_index, indexes in enumerate(groups):
        first_index = indexes[0]
        title_index = first_index - 1
        if title_index < 0:
            continue
        title = paragraphs[title_index].strip('“”" ')
        title = title.replace('” -', ' -').replace('" -', ' -')
        if not title or re.fullmatch(r'\d{4}\s*-\s*\d{4}\s+Season', title, re.I):
            continue

        next_title_index = (
            groups[group_index + 1][0] - 1 if group_index + 1 < len(groups) else len(paragraphs)
        )
        description = clean_text('\n\n'.join(paragraphs[title_index:next_title_index])) or None

        for index in indexes:
            match = DATE_RE.search(paragraphs[index])
            if not match:
                continue
            event_date = parse_date(match.group('date'))
            time_from = parse_time(match.group('time'))
            venue = clean_venue(match.group('venue'))
            if not event_date or not time_from or not venue:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': page_url,
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    for block in soup.select('.sqs-html-content'):
        records.extend(records_from_block(block, url))
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in (CONCERTS_URL, ARCHIVE_URL):
        try:
            records.extend(scrape_page(session, url))
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No dated concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return result


class LouisvilleCivicOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='louisvillecivicorchestra_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    LouisvilleCivicOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
