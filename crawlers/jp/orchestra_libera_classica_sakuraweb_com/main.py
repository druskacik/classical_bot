import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestra-libera-classica.sakuraweb.com/'
SOURCE = 'Orchestra Libera Classica'
UPCOMING_URL = urljoin(
    SOURCE_URL, 'blogs/blog_entries/index/29/limit:100?frame_id=58'
)
ARCHIVE_URL = urljoin(
    SOURCE_URL, 'bbses/bbs_articles/index/14/limit:100?frame_id=33'
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

# The archive uses consistent hall names but does not provide structured
# addresses. These are first-party venue names whose municipalities are stable.
VENUES = (
    ('三鷹市芸術文化センター風のホール', '三鷹市芸術文化センター 風のホール', 'Mitaka', 'JP'),
    ('三鷹市芸術文化センター 風のホール', '三鷹市芸術文化センター 風のホール', 'Mitaka', 'JP'),
    ('三鷹芸術文化センター 風のホール', '三鷹市芸術文化センター 風のホール', 'Mitaka', 'JP'),
    ('三鷹市芸術文化センター・風のホール', '三鷹市芸術文化センター 風のホール', 'Mitaka', 'JP'),
    ('めぐろパーシモンホール 大ホール', 'めぐろパーシモンホール 大ホール', 'Tokyo', 'JP'),
    ('第一生命ホール', '第一生命ホール', 'Tokyo', 'JP'),
    ('上野学園 石橋メモリアルホール', '上野学園 石橋メモリアルホール', 'Tokyo', 'JP'),
    ('上野学園石橋メモリアルホール', '上野学園 石橋メモリアルホール', 'Tokyo', 'JP'),
    ('パルテノン多摩5階シティーサロン', 'パルテノン多摩 5階シティーサロン', 'Tama', 'JP'),
    ('パルテノン多摩 小ホール', 'パルテノン多摩 小ホール', 'Tama', 'JP'),
    ('パルテノン多摩小ホール', 'パルテノン多摩 小ホール', 'Tama', 'JP'),
    ('パルテノン多摩 大ホール', 'パルテノン多摩 大ホール', 'Tama', 'JP'),
    ('パルテノン多摩大ホール', 'パルテノン多摩 大ホール', 'Tama', 'JP'),
    ('越後妻有文化ホール 段十ろう', '越後妻有文化ホール 段十ろう', 'Tokamachi', 'JP'),
    ('稲城市立iプラザホール', '稲城市立 iプラザホール', 'Inagi', 'JP'),
    ('みなとみらいホール大ホール', '横浜みなとみらいホール 大ホール', 'Yokohama', 'JP'),
    ('逗子文化プラザなぎさホール', '逗子文化プラザ なぎさホール', 'Zushi', 'JP'),
    ('いずみホール', '住友生命いずみホール', 'Osaka', 'JP'),
    ('Filharmonia Narodowa Sala Koncertowa', 'Filharmonia Narodowa Sala Koncertowa', 'Warsaw', 'PL'),
)

# This sole archive entry omits its year in both title and body. Its stable
# content key identifies the 2022 concert (between the dated 2021 and 2023
# entries, and 7 October was a Friday in 2022).
UNDATED_EVENT_YEARS = {
    '1f43aabc2758469f5e6ec9cec42865af': 2022,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text, fallback_year=None):
    match = re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if match:
        parts = map(int, match.groups())
    else:
        match = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})[日.,]', text)
        if not match or fallback_year is None:
            return None
        parts = (fallback_year, int(match.group(1)), int(match.group(2)))
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def parse_time(text):
    patterns = (
        r'(?:開演|昼公演|夜公演)?\s*([01]?\d|2[0-3])\s*[時:]\s*([0-5]\d)',
        r'\b([01]?\d|2[0-3]):([0-5]\d)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return f'{int(match.group(1)):02d}:{match.group(2)}'
    return None


def resolve_location(text):
    for marker, venue, city, country_code in VENUES:
        if marker in text:
            return venue, city, country_code
    return None


def build_record(title, body, url, fallback_year=None):
    title = clean_text(title).replace('\n', ' ')
    description = clean_text(body)
    combined = f'{title}\n{description}'
    event_date = parse_date(combined, fallback_year)
    location = resolve_location(combined)
    if not title or not event_date or not location:
        return None

    # This archive includes one lecture without an advertised substantial
    # performance. The rehearsal viewing is retained under project guidance.
    if '特別レクチャー' in title:
        return None

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(combined),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def expand_occurrences(record):
    """Split explicitly advertised same-day matinee/evening performances."""
    matches = re.findall(
        r'(?:昼公演|夜公演)\s*([01]?\d|2[0-3]):([0-5]\d)', record['title']
    )
    if len(matches) < 2:
        return [record]
    return [
        {**record, 'time_from': f'{int(hour):02d}:{minute}'}
        for hour, minute in matches
    ]


def fetch_archive_detail(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    article = soup.select_one('article.bbs-article')
    if article is None:
        return None
    return build_record(
        article.select_one(':scope > h1'),
        article.select_one('.bbs-article-body'),
        url,
        next((year for key, year in UNDATED_EVENT_YEARS.items() if key in url), None),
    )


class OrchestraLiberaClassicaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestra_libera_classica_sakuraweb_com',
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
            upcoming_response = session.get(UPCOMING_URL, timeout=45)
            upcoming_response.raise_for_status()
            archive_response = session.get(ARCHIVE_URL, timeout=45)
            archive_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Orchestra Libera Classica concert listings',
                event='crawler_fetch_failed', level='error', url=SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        upcoming_soup = BeautifulSoup(upcoming_response.text, 'html.parser')
        for article in upcoming_soup.select('article.blogs_entry'):
            title_link = article.select_one('.blogs_entry_title a[href]')
            body = article.select_one('.blogs_entry_body1')
            if title_link is None or body is None:
                continue
            posted = clean_text(article.select_one('.blogs_entry_meta'))
            posted_year = None
            year_match = re.search(r'(20\d{2})/', posted)
            if year_match:
                posted_year = int(year_match.group(1))
            record = build_record(
                title_link, body, urljoin(SOURCE_URL, title_link['href']), posted_year
            )
            if record:
                records.extend(expand_occurrences(record))

        archive_soup = BeautifulSoup(archive_response.text, 'html.parser')
        archive_urls = [
            urljoin(SOURCE_URL, link['href'])
            for link in archive_soup.select('article.bbs-root-list h2 a[href]')
        ]
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(fetch_archive_detail, url): url for url in archive_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Orchestra Libera Classica concert detail',
                        event='crawler_detail_fetch_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.extend(expand_occurrences(record))

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    OrchestraLiberaClassicaCrawler().run()


if __name__ == '__main__':
    main()
