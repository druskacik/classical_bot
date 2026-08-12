import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.londonearlyopera.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/avada_portfolio'
SOURCE = 'London Early Opera'
FEATURED_CATEGORY_ID = 4

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(\d{1,2})(?:st|nd|rd|th)?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'(?:\s+(20\d{2}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value, separator=' '):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def content_soup(rendered):
    soup = BeautifulSoup(rendered or '', 'html.parser')
    for node in soup.select('style, script, .fusion-button, .fusion-clearfix'):
        node.decompose()
    return soup


def parse_date(title, content):
    title_year = re.search(r'\b(20\d{2})\b', title)
    for text in (title, content):
        match = DATE_RE.search(text)
        if not match:
            continue
        year = match.group(3) or (title_year.group(1) if title_year else None)
        if not year:
            continue
        try:
            return datetime.strptime(
                f'{match.group(1)} {match.group(2)} {year}', '%d %B %Y'
            ).date().isoformat()
        except ValueError:
            continue
    return None


def normalise_time(match):
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_time(title, content):
    # A doors-opening time is not necessarily the advertised performance time.
    title_without_doors = re.sub(r'\bdoors?\s+open.*$', '', title, flags=re.IGNORECASE)
    if re.search(r'\d\s*/\s*\d', title_without_doors):
        return None
    twelve_hour = TIME_RE.search(title_without_doors)
    if twelve_hour:
        return normalise_time(twelve_hour)
    match = re.search(
        r'\b(?:concert|recital|performance)\s*(?:at\s*)?'
        r'(\d{1,2}):([0-5]\d)\b',
        content,
        re.IGNORECASE,
    )
    if match and int(match.group(1)) < 24:
        return f'{int(match.group(1)):02d}:{match.group(2)}'
    return None


def parse_location(lines):
    for line in lines:
        lower = line.lower()
        if 'the foundling museum' in lower:
            return 'The Foundling Museum', 'London', 'GB'
        if 'handel hendrix museum' in lower:
            return 'Handel Hendrix Museum', 'London', 'GB'
        if "st george's church" in lower or 'st george’s church' in lower:
            return "St George's Church, Hanover Square", 'London', 'GB'
        if "coram's fields" in lower or 'coram’s fields' in lower:
            return "Coram's Fields", 'London', 'GB'
        if 'the glucksman' in lower:
            return 'The Glucksman, UCC', 'Cork', 'IE'
        if 'église de saint rabier' in lower or 'eglise de saint rabier' in lower:
            return 'Église de Saint-Rabier', 'Saint-Rabier', 'FR'
    return None, None, None


def description_from_soup(soup):
    lines = []
    for line in clean_text(soup, '\n').splitlines():
        if not line or line.upper() == 'BACK':
            continue
        if re.match(r'^(?:tickets?|more information|read more|location)\b', line, re.I):
            continue
        if line not in lines:
            lines.append(line)
    return '\n'.join(lines) or None


def parse_event(item):
    url = clean_text(item.get('link'))
    title = clean_text(item.get('title', {}).get('rendered'))
    soup = content_soup(item.get('content', {}).get('rendered'))
    content = clean_text(soup, '\n')
    event_date = parse_date(title, content)
    venue, city, country_code = parse_location(content.splitlines())
    if not title or not url or not event_date or not venue or not city or not country_code:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(title, content),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from_soup(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LondonEarlyOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='londonearlyopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        items = []
        page = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'portfolio_category': FEATURED_CATEGORY_ID,
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'desc',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            items.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', 1))
            if page >= total_pages:
                break
            page += 1

        records = []
        for item in items:
            record = parse_event(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete London Early Opera concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(item.get('link')),
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, city, or country is missing',
                )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    LondonEarlyOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
