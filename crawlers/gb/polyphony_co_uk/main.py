import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.polyphony.co.uk/'
SITEMAP_URL = f'{SOURCE_URL}blog-posts-sitemap.xml'
SOURCE = 'Polyphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# The archive contains both the choir's British concerts and individual stops
# from its 2025 US tour. These names are printed by the source itself.
US_CITIES = {
    'Atlanta', 'Birmingham', 'Cleveland Heights', 'Dallas', 'Lincoln',
    'Nashville', 'New Haven', 'Palm Beach', 'Savannah',
}
UK_CITIES = {'London', 'Norwich', 'Saffron Walden', 'Tetbury'}

NON_EVENT_TITLES = re.compile(
    r'\b(?:recording|released|broadcast|radio|introducing|tour schedule|'
    r'to tour|honorary patron)\b',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(title, description):
    combined = f'{description}\n{title}'
    match = re.search(
        r'\b(\d{1,2})(?:st|nd|rd|th)?\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'(?:\s+(20\d{2}))?',
        combined,
        re.I,
    )
    if not match:
        return None
    year = match.group(3)
    if not year:
        year_match = re.search(r'\b(20\d{2})\b', title)
        year = year_match.group(1) if year_match else None
    if not year:
        return None
    try:
        return date(
            int(year), MONTHS[match.group(2).lower()], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_time(description):
    match = re.search(
        r'\b(\d{1,2})(?:\s*[.:]\s*(\d{1,2}))?\s*(am|pm)\b',
        description,
        re.I,
    )
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    minute = int(match.group(2) or 0)
    if minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def event_city(title, description):
    text = f'{title}\n{description}'
    for city in sorted(US_CITIES | UK_CITIES, key=len, reverse=True):
        if re.search(rf'\b{re.escape(city)}\b', text, re.I):
            return city
    return None


def event_venue(description, city):
    lines = [line.strip(' ,') for line in description.splitlines() if line.strip(' ,')]
    city_index = next(
        (i for i, line in enumerate(lines) if re.search(rf'\b{re.escape(city)}\b', line, re.I)),
        None,
    )
    if city_index is not None:
        line = lines[city_index]
        # Saffron Hall's venue and address are separate blocks.
        if line.lower().startswith(('audley end road', 'london,', 'nashville,', 'norwich,')):
            return lines[city_index - 1] if city_index else None
        venue = re.split(rf',\s*{re.escape(city)}\b', line, maxsplit=1, flags=re.I)[0]
        if venue and venue.lower() != city.lower():
            return venue

    # Savannah is present in the title but omitted from its venue line.
    date_index = next(
        (i for i, line in enumerate(lines) if re.search(r'\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+20\d{2}\b', line)),
        None,
    )
    if date_index is not None and date_index + 1 < len(lines):
        candidate = lines[date_index + 1]
        if not re.match(r'https?://', candidate) and candidate.lower() != 'polyphony':
            return candidate
    return None


def parse_post(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('[data-hook="post-title"]'))
    description = clean_text(soup.select_one('[data-hook="post-description"]'))
    if not title or not description or NON_EVENT_TITLES.search(title):
        return None

    event_date = parse_date(title, description)
    city = event_city(title, description)
    venue = event_venue(description, city) if city else None
    if not event_date or not city or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': 'US' if city in US_CITIES else 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_post(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_post(response.text, url)


class PolyphonyCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='polyphony_co_uk',
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
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.text, 'xml')
        urls = [
            clean_text(node)
            for node in sitemap.select('url > loc')
            if '/post/' in clean_text(node)
        ]

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_post, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Polyphony post',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    PolyphonyCoUkCrawler().run()


if __name__ == '__main__':
    main()
