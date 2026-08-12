import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hkchamber.org/'
SOURCE = 'Hong Kong Chamber Orchestra'
API_URL = SOURCE_URL
UPCOMING_SLUG = 'upcoming-concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-HK,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def clean_text(node):
    if node is None:
        return ''
    text = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value, links):
    match = re.search(
        r'\b(\d{1,2})\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{4})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS[match.group(2).lower()]
    year = int(match.group(3))

    # The source has occasionally left a stale year in its prose while linking
    # to a first-party registration page whose URL carries the corrected date.
    for link in links:
        iso_match = re.search(r'(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)', link)
        if iso_match and (int(iso_match.group(2)), int(iso_match.group(3))) == (month, day):
            year = int(iso_match.group(1))
            break

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def parse_upcoming_post(post):
    soup = BeautifulSoup(post['content']['rendered'], 'html.parser')
    blocks = [node for node in soup.find_all(['p', 'div']) if clean_text(node)]
    records = []

    for title_node in soup.find_all(['strong', 'b']):
        title = clean_text(title_node)
        title_block = title_node.find_parent(['p', 'div'])
        if not title or title_block not in blocks:
            continue

        start = blocks.index(title_block)
        following = blocks[start + 1:start + 9]
        date_index = next(
            (index for index, node in enumerate(following)
             if re.search(r'\b\d{1,2}\s+[A-Za-z]+\s+20\d{2}\b', clean_text(node))),
            None,
        )
        if date_index is None or date_index + 1 >= len(following):
            continue

        date_node = following[date_index]
        venue_node = following[date_index + 1]
        links = [link.get('href', '') for link in soup.find_all('a', href=True)]
        event_date = parse_date(clean_text(date_node), links)
        venue = clean_text(venue_node).split(',', 1)[0].strip()
        if not event_date or not venue:
            continue

        description_parts = []
        for node in following[date_index + 2:]:
            text = clean_text(node)
            if re.search(r'^(register|book|buy|follow us|tickets?)\b', text, re.IGNORECASE):
                break
            if text and text not in description_parts:
                description_parts.append(text)

        records.append({
            'title': title,
            'date': event_date,
            'url': post['link'],
            'time_from': parse_time(clean_text(date_node)),
            'venue': venue,
            'city': 'Hong Kong',
            'country_code': 'HK',
            'description': ' '.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


class HkchamberOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hkchamber_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HK',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        try:
            response = requests.get(
                API_URL,
                params={
                    'rest_route': '/wp/v2/posts',
                    'slug': UPCOMING_SLUG,
                    'per_page': 100,
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            posts = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Hong Kong Chamber Orchestra concerts',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            records.extend(parse_upcoming_post(post))
        return records


def main():
    return HkchamberOrgCrawler().run()


if __name__ == '__main__':
    main()
