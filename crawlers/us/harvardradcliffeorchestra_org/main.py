import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.harvardradcliffeorchestra.org/'
SOURCE = 'Harvard-Radcliffe Orchestra'
CONCERTS_URL = f'{SOURCE_URL}concerts'
TOUR_URL = f'{SOURCE_URL}tour'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)).strip()


def parse_date(value, year=None):
    text = clean_text(value)
    if year and not re.search(r'\b\d{4}\b', text):
        text = f'{text}, {year}'
    text = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*', '', text)
    text = re.sub(r',?\s*\d{1,2}:\d{2}\s*[ap]m.*$', '', text, flags=re.I)
    for pattern in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}(?::\d{2})?)\s*([ap]m)\b', clean_text(value), re.I)
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(f'{match.group(1)} {match.group(2)}', pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def concert_records(soup):
    records = []
    for container in soup.select('.concert-containers'):
        children = container.find_all(recursive=False)
        for index, child in enumerate(children):
            heading = child.select_one('.heading-30')
            details = child.select_one('.text-block-25')
            if not heading or not details:
                continue

            lines = [clean_text(part) for part in details.stripped_strings if clean_text(part)]
            event_date = next((parse_date(line) for line in lines if parse_date(line)), None)
            time_from = next((parse_time(line) for line in lines if parse_time(line)), None)
            venue = next(
                (line for line in lines if not parse_date(line) and not parse_time(line)),
                None,
            )
            if not event_date or not venue:
                continue

            programme = []
            for sibling in children[index + 1:]:
                if sibling.select_one('.heading-30'):
                    break
                text = clean_text(sibling)
                if text and text not in programme:
                    programme.append(text)

            records.append({
                'title': clean_text(heading).rstrip(':'),
                'date': event_date,
                'url': CONCERTS_URL,
                'time_from': time_from,
                'venue': venue,
                'city': 'Cambridge' if venue == 'Sanders Theatre' else None,
                'country_code': 'US',
                'description': '\n'.join(programme) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return [record for record in records if record['title'] and record['city']]


def tour_records(soup):
    page_text = clean_text(soup)
    year_match = re.search(r'\b(?:In\s+)?(?:May\s+)?(20\d{2})\b[^.]{0,100}\b(?:Japan|tour)\b', page_text, re.I)
    if not year_match:
        return []
    year = year_match.group(1)
    concerts_section = next(
        (
            section for section in soup.select('.div-block-105')
            if clean_text(section.select_one('h1')).lower() == 'concerts'
        ),
        None,
    )
    description_node = concerts_section.select_one('.text-block-40') if concerts_section else None
    description = clean_text(description_node) or None

    records = []
    grid = soup.select_one('.grid-7')
    if not grid:
        return records
    children = grid.find_all(recursive=False)
    for index in range(0, len(children) - 1, 2):
        place, timing = children[index:index + 2]
        city = clean_text(place.select_one('.tour-location'))
        venue = clean_text(place.select_one('.tour-venue'))
        timing_text = clean_text(timing)
        date_match = re.search(r'([A-Za-z]+\s+\d{1,2})', timing_text)
        event_date = parse_date(date_match.group(1), year) if date_match else None
        if not city or not venue or not event_date:
            continue
        records.append({
            'title': f'Harvard-Radcliffe Orchestra in {city}',
            'date': event_date,
            'url': TOUR_URL,
            'time_from': parse_time(timing_text),
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url, parser in ((CONCERTS_URL, concert_records), (TOUR_URL, tour_records)):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parser(BeautifulSoup(response.text, 'html.parser')))
        except requests.RequestException as error:
            log_message(
                'Event page request failed',
                event='crawler_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No concrete concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class HarvardRadcliffeOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='harvardradcliffeorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    HarvardRadcliffeOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
