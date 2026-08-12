import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wexfordopera.com/'
CALENDAR_URL = f'{SOURCE_URL}programme/festival-programme/calendar?list=1'
SOURCE = 'Wexford Festival Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-IE,en;q=0.9',
}

# These are the site's own programme disciplines which consistently describe
# a live opera, recital, choral, or other classical performance. The adjacent
# "Talks" and "Events" disciplines contain tours, parties, and interviews.
IN_SCOPE_CATEGORIES = {
    'Community Opera',
    'Concerts and Recitals',
    'Main Stage Opera',
    'Night Opera',
    'Pocket Opera / Opera Beag',
    'Wexford Factory',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
         'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'),
        1,
    )
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(url):
    last_error = None
    for attempt in range(4):
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise last_error


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    year_match = re.search(r'\b(20\d{2})\b', clean_text(soup.title))
    if not year_match:
        raise ValueError('Festival year was not found in the calendar title')

    event_urls = []
    for row in soup.select('#list-calendar .row'):
        title_link = row.select_one('h3.title a[href*="/programme/festival-programme/"]')
        details = title_link.find_parent('div') if title_link else None
        if not title_link or not details:
            continue
        details_text = clean_text(details)
        title = clean_text(title_link)
        category = details_text.removeprefix(title).split(',', 1)[0].strip()
        if category in IN_SCOPE_CATEGORIES:
            event_urls.append(title_link['href'])

    if not event_urls:
        raise ValueError('No in-scope event links were found in the festival calendar')
    return int(year_match.group(1)), list(dict.fromkeys(event_urls))


def parse_date(value, year):
    match = re.search(r'\b(\d{1,2})\s+([A-Z][a-z]{2})\b', value)
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*([ap])m', value.strip(), re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def event_description(soup):
    blocks = []
    for block in soup.select('#overview .block-text .text-wrapper'):
        text = clean_text(block)
        if text and text not in blocks:
            blocks.append(text)
    return '\n\n'.join(blocks) or None


def page_venue(soup):
    for label in soup.select('main strong'):
        if clean_text(label).lower() != 'venue':
            continue
        container = label.find_parent('div')
        if not container:
            continue
        value = container.find('div', recursive=False)
        venue = clean_text(value)
        if venue:
            return venue
    return ''


def parse_event(url, html, year):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    title_node = main.select_one('h1') if main else None
    title = clean_text(title_node) if title_node else ''
    description = event_description(soup)
    default_venue = page_venue(soup)
    records = []

    for row in soup.select('table.table-booking tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 3:
            continue
        event_date = parse_date(clean_text(cells[0]), year)
        time_from = parse_time(clean_text(cells[1]))
        venue = clean_text(cells[2]) or default_venue
        if not all((title, event_date, time_from, venue)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Wexford',
            'country_code': 'IE',
            'description': description,
        })

    return records


class WexfordoperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wexfordopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        year, urls = parse_calendar(get_page(CALENDAR_URL))
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(get_page, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    event_records = parse_event(url, future.result(), year)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Wexford Festival Opera event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if not event_records:
                    log_message(
                        'Skipped event without complete occurrences',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='No occurrence had a valid date, time, and venue',
                    )
                records.extend(event_records)

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'], item['title'], item['venue']
            ),
        )


def main():
    WexfordoperaComCrawler().run()


if __name__ == '__main__':
    main()
