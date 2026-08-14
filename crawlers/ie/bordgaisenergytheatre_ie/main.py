import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bordgaisenergytheatre.ie/'
SITEMAP_URL = f'{SOURCE_URL}show-sitemap.xml'
SOURCE = 'Bord Gáis Energy Theatre'
VENUE = 'Bord Gáis Energy Theatre'
CITY = 'Dublin'

HEADERS = {
    'Accept-Language': 'en-IE,en;q=0.9',
}

DATE_FORMATS = (
    '%A %d %B %Y',
    '%d %B %Y',
)


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
            response = requests.get(
                url,
                headers=HEADERS,
                impersonate='chrome',
                timeout=45,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestsError as error:
            last_error = error
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise last_error


def show_urls(html):
    soup = BeautifulSoup(html, 'xml')
    urls = [clean_text(node) for node in soup.select('url loc')]
    urls = [url for url in urls if '/show/' in url]
    if not urls:
        raise ValueError('No show URLs were found in the show sitemap')
    return list(dict.fromkeys(urls))


def parse_date(value):
    value = clean_text(value)
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)\b', '', value, flags=re.I)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value.title(), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])m\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def archived_start_date(value):
    """Return the first explicitly advertised date from an archived run."""
    match = re.search(
        r'\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+[A-Za-z]+)?\s*(?:-|–|—|to)\s*'
        r'\d{1,2}(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(20\d{2})\b',
        clean_text(value),
        re.I,
    )
    if match:
        return parse_date(f'{match.group(1)} {match.group(2)} {match.group(3)}')
    match = re.search(r'\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+20\d{2}\b', clean_text(value), re.I)
    return parse_date(match.group(0)) if match else None


def event_description(soup):
    blocks = []
    for block in soup.select(
        '.show-description__wrapper .cms-editor, '
        '.show-images__wrapper .cms-editor'
    ):
        text = clean_text(block)
        if text and text not in blocks:
            blocks.append(text)
    return '\n\n'.join(blocks) or None


def parse_event(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1.intro__title'))
    if not title:
        return []

    description = event_description(soup)
    occurrences = []
    for item in soup.select('main .upcoming-show__wrapper'):
        event_date = parse_date(clean_text(item.select_one('.upcoming-show__date')))
        if not event_date:
            continue
        occurrences.append((event_date, parse_time(clean_text(item))))

    # Once sales close, the site removes occurrence rows but keeps the archived
    # production page and its run date. Its first date is an advertised concrete
    # performance date, although the old performance time is no longer exposed.
    if not occurrences:
        event_date = archived_start_date(clean_text(soup.select_one('.intro__upper-text')))
        if event_date:
            occurrences.append((event_date, None))

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'IE',
            'description': description,
        }
        for event_date, time_from in occurrences
    ]


class BordgaisenergytheatreIeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bordgaisenergytheatre_ie',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        urls = show_urls(get_page(SITEMAP_URL))
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(get_page, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_event(url, future.result()))
                except requests.RequestsError as error:
                    log_message(
                        'Failed to scrape theatre show',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    BordgaisenergytheatreIeCrawler().run()


if __name__ == '__main__':
    main()
