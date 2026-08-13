import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tcvi.it/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Teatro Comunale Città di Vicenza'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# These first-party disciplines contain definite classical events and adjacent
# candidates (dance, family opera, musicals and hosted events) which need the
# potential-event classifier's event-by-event judgement.
CANDIDATE_CATEGORIES = {
    'concertistica', 'sinfonica', 'sinfonica-fuori-abbonamento', 'operetta',
    'opera-baby', 'opera-kids', 'opera-domani', 'musica', 'danza',
    'danza-fuori-abbonamento', 'danza-in-rete', 'danza-in-rete-off',
    'luoghi-del-contemporaneo-danza', 'musical', 'family-show',
    'spettacoli-per-le-scuole', 'eventi-ospitati',
    'ospitati-stagioni-precedenti',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def candidate_urls(sitemap):
    urls = []
    for node in sitemap.find_all('loc'):
        url = clean_text(node)
        path_parts = [part for part in urlparse(url).path.split('/') if part]
        if len(path_parts) < 5 or path_parts[0] != 'it' or path_parts[1] != 'eventi':
            continue
        if not CANDIDATE_CATEGORIES.intersection(path_parts):
            continue
        # Category/season landing pages do not contain a concrete occurrence.
        category_index = max(
            (index for index, part in enumerate(path_parts) if part in CANDIDATE_CATEGORIES),
            default=-1,
        )
        if category_index >= 0 and len(path_parts) > category_index + 1:
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_date(value, fallback_year=None):
    match = re.search(
        r'\b(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)'
        r'(?:\s+(\d{4}))?',
        value.casefold(),
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else fallback_year
    if year is None:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_detail(soup, url):
    title_node = soup.select_one('h1.uk-heading-primary')
    date_node = soup.select_one('main time.uk-margin-remove, time.uk-margin-remove')
    title = clean_text(title_node)
    heading_date = parse_date(clean_text(date_node))
    if not title or not heading_date:
        return []

    description_parts = []
    for selector in ('#program_notes', '#tab-spettacoli', '.event-description'):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text and text not in description_parts:
            description_parts.append(text)
    description = clean_text('\n\n'.join(description_parts)) or None

    records = []
    for row in soup.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        date_text = clean_text(cells[0])
        event_date = parse_date(date_text, int(heading_date[:4]))
        venue = clean_text(cells[1])
        if not event_date or not venue:
            continue
        time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', date_text)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
            'venue': venue,
            'city': 'Vicenza',
            'country_code': 'IT',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class TcviItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tcvi_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        sitemap = get_soup(SITEMAP_URL)
        urls = candidate_urls(sitemap)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(get_soup, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_detail(future.result(), url))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch or parse TCVI event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    TcviItCrawler().run()


if __name__ == '__main__':
    main()
