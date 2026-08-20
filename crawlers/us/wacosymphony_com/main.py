import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wacosymphony.com/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets/schedule-tickets/')
SOURCE = 'Waco Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*', '', value)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time_location(value):
    parts = re.split(r'\s*//\s*', clean_text(value), maxsplit=1)
    if len(parts) != 2 or not parts[1]:
        return None, None

    time_value = re.sub(r'\.', '', parts[0]).strip().upper()
    time_from = None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            time_from = datetime.strptime(time_value, pattern).strftime('%H:%M')
            break
        except ValueError:
            pass
    return time_from, parts[1].strip()


def detail_records(soup, url):
    heading = soup.select_one('h1.mainHeading1.textBlue')
    title_parts = list(dict.fromkeys(clean_text(part) for part in heading.stripped_strings)) if heading else []
    title_parts = [part for part in title_parts if part]
    title = ' — '.join(title_parts)
    if not title:
        return []

    description_parts = []
    description = clean_text(soup.select_one('.performanceDesc'))
    if description:
        description_parts.append(description)

    repertoire_heading = next(
        (item for item in soup.select('h3') if clean_text(item).lower() == 'repertoire'),
        None,
    )
    if repertoire_heading:
        repertoire = clean_text(repertoire_heading.parent)
        if repertoire and repertoire not in description_parts:
            description_parts.append(repertoire)

    records = []
    for date_node in soup.select('.performanceDate'):
        event_date = parse_date(clean_text(date_node))
        time_node = date_node.find_next_sibling(class_='performanceTimeLocation')
        time_from, venue = parse_time_location(time_node)
        if not event_date or not venue:
            continue

        # The public performance calendar is for Waco concerts. Waco Hall is
        # the orchestra's documented concert venue and all current occurrences
        # use it; a detail page with a different explicit city can be added when
        # the source begins publishing touring dates.
        if venue.lower() != 'waco hall':
            log_message(
                'Skipping performance with an unmapped venue',
                event='crawler_record_skipped',
                level='warning',
                url=url,
                venue=venue,
            )
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Waco',
            'country_code': 'US',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class WacoSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wacosymphony_com',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(LISTING_URL, timeout=45)
            response.raise_for_status()
            listing_soup = BeautifulSoup(response.text, 'html.parser')
            urls = list(dict.fromkeys(
                urljoin(LISTING_URL, link['href'])
                for link in listing_soup.select('a[href*="/performances/"][href]')
            ))

            records = []
            for url in urls:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                records.extend(detail_records(BeautifulSoup(detail_response.text, 'html.parser'), url))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Waco Symphony performances',
                event='crawler_fetch_failed',
                level='error',
                url=LISTING_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not urls:
            log_message(
                'No performance links found',
                event='crawler_empty_listing',
                level='warning',
                url=LISTING_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    WacoSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
