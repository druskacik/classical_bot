import re
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kirishima-imf.jp/'
SOURCE = 'Kirishima International Music Festival'
LIST_URL = urljoin(SOURCE_URL, 'concert-list')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja,en;q=0.7',
}

VENUE_CITIES = {
    'みやまコンセール': 'Kirishima',
    '宝山ホール': 'Kagoshima',
    '鹿児島県庁2階県民ホール': 'Kagoshima',
    '鹿児島空港ビル': 'Kirishima',
    '霧島神宮': 'Kirishima',
    '種子島こりーな': 'Nakatane',
    'marukawaホール': 'Minamikyushu',
    '茶音の蔵': 'Shibushi',
    '伊集院文化会館': 'Hioki',
    'ザビエル教会': 'Kagoshima',
    '湧水町いきいきセンター くりの郷': 'Yusui',
    '天城町防災センター': 'Amagi',
}

EXCLUDED_EVENT_TYPES = ('オープン・レッスン', '公開レッスン')


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalize_for_match(value):
    return re.sub(r'[^0-9A-Za-z\u3040-\u30ff\u3400-\u9fff]+', '', value).lower()


def festival_year(soup):
    text = clean_text(soup)
    patterns = (
        r'第\d+回霧島国際音楽祭\s*(20\d{2})',
        r'霧島国際音楽祭\s*(20\d{2})',
        r'/uploads/(20\d{2})/',
    )
    html = str(soup)
    for pattern in patterns:
        match = re.search(pattern, text if 'uploads' not in pattern else html)
        if match:
            return int(match.group(1))
    raise ValueError('Could not determine the festival year')


def venue_city(venue):
    normalized = re.sub(r'\s+', ' ', venue).strip()
    for known_venue, city in VENUE_CITIES.items():
        if known_venue.lower() in normalized.lower():
            return city
    return None


def detail_sections(soup):
    sections = []
    seen = set()
    for anchor in soup.select('.elementor-menu-anchor[id]'):
        if not re.fullmatch(r'\d{1,2}-\d{1,2}(?:-\d+)?', anchor.get('id', '')):
            continue
        section = anchor.find_parent('section')
        if section is None or id(section) in seen:
            continue
        seen.add(id(section))
        text = clean_text(section)
        date_match = re.search(r'(20\d{2})年\s*(\d{1,2})/(\d{1,2})', text)
        if date_match:
            sections.append({
                'date': '-'.join((date_match.group(1), date_match.group(2).zfill(2),
                                  date_match.group(3).zfill(2))),
                'text': text,
            })
    return sections


def best_description(sections, event_date, title):
    candidates = [section for section in sections if section['date'] == event_date]
    if not candidates:
        return None
    normalized_title = normalize_for_match(title)
    best = max(
        candidates,
        key=lambda section: SequenceMatcher(
            None, normalized_title, normalize_for_match(section['text'])
        ).ratio(),
    )
    return best['text'] or None


def parse_listing(soup, year):
    records = []
    for link in soup.select('section.elementor-inner-section a[href*="#"]'):
        title = clean_text(link)
        if not title or any(label in title for label in EXCLUDED_EVENT_TYPES):
            continue

        section = link.find_parent('section', class_='elementor-inner-section')
        if section is None:
            continue
        columns = section.select(':scope > .elementor-container > .elementor-column')
        if len(columns) < 3:
            continue
        time_text = clean_text(columns[0])
        venue = clean_text(columns[1])
        city = venue_city(venue)
        time_match = re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', time_text)

        day_section = section.find_parent('section', class_='elementor-top-section')
        day_anchor = day_section.select_one('.elementor-menu-anchor[id]') if day_section else None
        day_match = re.fullmatch(r'(\d{1,2})-(\d{1,2})', day_anchor.get('id', '')) if day_anchor else None
        if not day_match or not venue or not city:
            continue
        try:
            event_date = date(year, int(day_match.group(1)), int(day_match.group(2))).isoformat()
        except ValueError:
            continue

        time_from = None
        if time_match:
            hour, minute = time_match.group(0).split(':')
            time_from = f'{int(hour):02d}:{minute}'

        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(LIST_URL, link['href']),
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class KirishimaImfJpCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kirishima_imf_jp',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(LIST_URL, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            records = parse_listing(soup, festival_year(soup))

            detail_urls = sorted({urldefrag(record['url']).url for record in records})
            details = {}
            for detail_url in detail_urls:
                detail_response = session.get(detail_url, timeout=45)
                detail_response.raise_for_status()
                details[detail_url] = detail_sections(
                    BeautifulSoup(detail_response.text, 'html.parser')
                )
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Kirishima festival concerts',
                event='crawler_fetch_failed',
                level='error',
                url=LIST_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        for record in records:
            detail_url = urldefrag(record['url']).url
            record['description'] = best_description(
                details.get(detail_url, []), record['date'], record['title']
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['venue'], record['title']
            ),
        )


def main():
    KirishimaImfJpCrawler().run()


if __name__ == '__main__':
    main()
