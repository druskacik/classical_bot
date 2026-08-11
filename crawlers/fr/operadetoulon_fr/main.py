import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operadetoulon.fr/'
SOURCE = 'Opéra de Toulon'
CALENDAR_URL = urljoin(SOURCE_URL, 'fr/calendrier.htm')
ARCHIVE_URL = urljoin(SOURCE_URL, 'fr/archives-spectacles.htm')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def folded(value):
    return ''.join(
        char for char in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(char)
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_links(soup):
    return {
        canonical_url(link.get('href'))
        for link in soup.select('a[href*="/spectacles/"]')
        if re.search(r'/spectacles/.+/\d+\.htm$', canonical_url(link.get('href')))
    }


def discover_urls():
    calendar = get_soup(CALENDAR_URL)
    urls = event_links(calendar)

    first_archive = get_soup(ARCHIVE_URL)
    urls.update(event_links(first_archive))
    total_match = re.search(r'(\d+)\s+spectacles', clean_text(first_archive), re.I)
    # Archive pages currently contain 13 cards. Derive the complete page count
    # from the site's published total so older pagination pages are not missed.
    total = int(total_match.group(1)) if total_match else 0
    page_count = math.ceil(total / 13) if total else 1
    archive_pages = [f'{ARCHIVE_URL}?page={page}' for page in range(1, page_count)]
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, url): url for url in archive_pages}
        for future in as_completed(futures):
            page_url = futures[future]
            try:
                urls.update(event_links(future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Opéra de Toulon archive page',
                    event='crawler_page_failed',
                    level='warning',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(urls)


def parse_occurrence(value):
    text = clean_text(value)
    match = re.search(
        r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})(?:\s+(\d{1,2})\s*[Hh:]\s*(\d{2}))?',
        text,
    )
    if not match:
        return None
    month = MONTHS.get(folded(match.group(2)))
    if not month:
        return None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(4):
        hour, minute = int(match.group(4)), int(match.group(5))
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def parse_location(node):
    value = clean_text(node)
    parts = [part.strip(' -') for part in re.split(r'\s+-\s+', value) if part.strip(' -')]
    if len(parts) < 2:
        return None
    venue = parts[0]
    city = parts[-1]
    if not venue or not city or folded(venue) == folded(city):
        return None
    return venue, city


def section_text(soup, heading):
    wanted = folded(heading)
    title = next(
        (
            node for node in soup.select('h2')
            if folded(clean_text(node)).strip() == wanted
            or folded(clean_text(node)).strip().startswith(wanted + ' ')
        ),
        None,
    )
    if not title:
        return ''
    panel = title.find_next_sibling(class_=re.compile(r'panel-collapse|panel-body'))
    if panel is None and title.parent:
        panel = title.parent.select_one('.panel-body')
    return clean_text(panel)


def parse_detail(url):
    soup = get_soup(url)
    title = clean_text(soup.find('h1'))
    dates_node = soup.select_one('.datesDETAIL')
    location_node = dates_node.select_one('.lieu') if dates_node else None
    location = parse_location(location_node)
    if not title or not dates_node or not location:
        return []

    venue, city = location
    presentation = section_text(soup, 'Présentation')
    programme = section_text(soup, 'Programme')
    description = '\n\n'.join(part for part in (programme, presentation) if part) or None
    records = []
    for item in dates_node.select('li'):
        occurrence = parse_occurrence(item)
        if occurrence:
            event_date, time_from = occurrence
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'FR',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class OperaDeToulonFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operadetoulon_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        urls = discover_urls()
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(parse_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Opéra de Toulon event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    OperaDeToulonFrCrawler().run()


if __name__ == '__main__':
    main()
