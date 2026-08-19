import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchestrasulponticello.org/'
SOURCE = 'Decatur Orchestra Sul Ponticello'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def clean_text(value):
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value or '')
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def season_years(soup):
    title = clean_text(soup.title)
    match = re.search(r'(20\d{2})\s*[-–—]\s*(20\d{2})\s+SEASON', title, re.I)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def parse_event_date(value, start_year, end_year):
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
        r'([A-Za-z]+)\s+(\d{1,2})(?:,\s*(20\d{2}))?',
        value,
        re.I,
    )
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    year = int(match.group(3)) if match.group(3) else None
    if year is None and start_year and end_year:
        year = start_year if month >= 7 else end_year
    if year is None:
        return None
    try:
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2}):([0-5]\d)\s*([ap])\.?m\.?', value.strip(), re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def is_event_marker(element):
    return element.name == 'h1' and bool(
        re.fullmatch(r'(?:CONCERT\s+\d+|SPECIAL STUDENT CONCERT)', clean_text(element), re.I)
    )


def parse_season_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []
    start_year, end_year = season_years(soup)
    elements = main.select('h1, h2, h3, h4, p')
    markers = [index for index, element in enumerate(elements) if is_event_marker(element)]
    records = []

    for marker_number, start in enumerate(markers):
        end = markers[marker_number + 1] if marker_number + 1 < len(markers) else len(elements)
        block = elements[start + 1:end]
        texts = [clean_text(element) for element in block]
        texts = [text for text in texts if text]
        title = next((clean_text(element).strip('“”" ') for element in block if element.name == 'h2'), '')
        event_date = next(
            (parsed for text in texts if (parsed := parse_event_date(text, start_year, end_year))),
            None,
        )
        time_from = next((parsed for text in texts if (parsed := parse_time(text))), None)
        venue = next((text for text in texts if re.search(r'\b(?:church|hall|theatre|theater|center)\b', text, re.I)), '')
        if venue.lower() == 'southside baptist church':
            venue = 'Southside Baptist Church'
        address = next((text for text in texts if re.search(r'\bDecatur\s*,?\s*AL\b', text, re.I)), '')
        city = 'Decatur' if address or venue.lower() == 'southside baptist church' else ''

        if not title or not event_date or not venue or not city:
            log_message(
                'Skipped incomplete Orchestra Sul Ponticello concert',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
                error_message='Required date, title, venue, or city is missing',
            )
            continue

        excluded = {address, venue}
        description_parts = []
        for text in texts:
            if text in excluded or parse_event_date(text, start_year, end_year) or parse_time(text):
                continue
            if text not in description_parts:
                description_parts.append(text)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': '\n\n'.join(description_parts) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def season_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for link in soup.select('a[href]'):
        if not re.search(r'20\d{2}\s*[-–—]\s*20\d{2}\s+SEASON', clean_text(link), re.I):
            continue
        url = urljoin(SOURCE_URL, link['href'])
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
            urls.add(url.split('#', 1)[0])
    return sorted(urls)


class OrchestraSulPonticelloOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestrasulponticello_org',
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
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = []
        for url in season_urls(response.text):
            try:
                page = requests.get(url, headers=HEADERS, timeout=45)
                page.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Orchestra Sul Ponticello season page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_season_page(page.text, url))
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    OrchestraSulPonticelloOrgCrawler().run()


if __name__ == '__main__':
    main()
