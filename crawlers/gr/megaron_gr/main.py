import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.megaron.gr/'
SOURCE = 'Megaron – The Athens Concert Hall'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'el-GR,el;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def discover_event_urls():
    soup = BeautifulSoup(get_page(SITEMAP_URL), 'xml')
    # The sitemap contains both Greek and translated versions of the same
    # events. Prefer the canonical Greek pages to avoid duplicate occurrences.
    return sorted({
        clean_text(node)
        for node in soup.select('url > loc')
        if '/event/' in clean_text(node) and '/en/event/' not in clean_text(node)
    })


def labelled_value(soup, labels):
    for item in soup.select('.block-2-lists li'):
        left = item.select_one('.left')
        right = item.select_one('.right')
        label = clean_text(left)
        if right and any(label.startswith(expected) for expected in labels):
            return right
    return None


def infer_city(venue, venue_node):
    folded = venue.casefold()
    if 'προκόπι' in folded:
        return 'Prokopi'
    if 'λίμνη' in folded or 'limni' in folded:
        return 'Limni'
    if 'μαντουδ' in folded or 'κυμάσι' in folded:
        return 'Mantoudi'

    href = ''
    link = venue_node.select_one('a[href]') if venue_node else None
    if link:
        href = link.get('href', '')
    athens_markers = (
        'aithousa-', 'kipos-tou-megarou', 'musixlab', 'ekthesiakoi-xoroi',
        'aithrio-', 'yp69', 'banquet', 'synedrion',
    )
    if '/venue/' in href and any(marker in href.casefold() for marker in athens_markers):
        return 'Athens'
    if any(marker in folded for marker in ('λαμπράκ', 'τριάντη', 'μητρόπουλ', 'σκαλκώτα', 'musixlab')):
        return 'Athens'
    return ''


def parse_occurrences(value):
    if not value:
        return []
    text = clean_text(value)
    occurrences = []
    # Detail pages enumerate each bookable occurrence in this field. Avoid
    # turning display ranges or season spans into invented daily events.
    for day, month, year, hour, minute in re.findall(
        r'(?<![-.\d])(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s*[-,]\s*(\d{1,2}):(\d{2}))?',
        text,
    ):
        try:
            event_date = datetime(int(year), int(month), int(day)).date().isoformat()
        except ValueError:
            continue
        time_from = f'{int(hour):02d}:{minute}' if hour else None
        occurrence = (event_date, time_from)
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    return occurrences


def parse_event(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    intro = soup.select_one('.type-event .block-intro')
    title_node = intro.select_one('h1') if intro else None
    title = clean_text(title_node)
    venue_node = labelled_value(soup, ('ΑΙΘΟΥΣΑ', 'ΧΩΡΟΣ'))
    venue = clean_text(venue_node)
    city = infer_city(venue, venue_node)
    occurrence_node = labelled_value(soup, ('ΗΜΕΡΕΣ ΚΑΙ ΩΡΕΣ', 'ΗΜΕΡΑ ΚΑΙ ΩΡΑ'))
    occurrences = parse_occurrences(occurrence_node)

    if not occurrences and intro:
        occurrences = parse_occurrences(intro.select_one('h2'))
    if not title or not venue or not city or not occurrences:
        return []

    description_parts = []
    for selector in ('.post-content', '.programmes-container', '.contributors-container'):
        for node in soup.select(selector):
            text = clean_text(node)
            if text and text not in description_parts:
                description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'GR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
    ]


def get_concerts():
    urls = discover_event_urls()
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_page, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Megaron event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MegaronGrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='megaron_gr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MegaronGrCrawler().run()


if __name__ == '__main__':
    main()
