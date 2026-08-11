import html
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.classiqueaularge.fr/'
SOURCE = 'Classique au large'
SEASONS_SITEMAP = urljoin(SOURCE_URL, 'wp-sitemap-taxonomies-saisons-1.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'decembre': 12,
}


def clean_text(node):
    if node is None:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def season_urls(session):
    response = session.get(SEASONS_SITEMAP, timeout=45)
    response.raise_for_status()
    urls = []
    for location in re.findall(r'<loc>(.*?)</loc>', response.text, re.DOTALL):
        url = html.unescape(location).strip()
        if re.search(r'/saison/saison-\d{4}/?$', url):
            urls.append(url)
    return sorted(set(urls))


def parse_date_time(value, year):
    text = clean_text(value).casefold()
    match = re.search(r'\b(\d{1,2})\s+([a-zà-ÿ]+)\b', text)
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b(\d{1,2})\s*h\s*(\d{2})?\b', text)
    if not time_match:
        return event_date, None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if hour > 23 or minute > 59:
        return event_date, None
    return event_date, f'{hour:02d}:{minute:02d}'


def description_for(article):
    parts = []
    for selector in ('.chapo', '.programme', '.presentation'):
        text = clean_text(article.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_article(article, year, page_url):
    title = clean_text(article.select_one('.titre'))
    venue = clean_text(article.select_one('.lieu'))
    event_date, time_from = parse_date_time(article.select_one('.date'), year)
    post_id = clean_text(article.get('id'))
    if not all((title, venue, event_date, post_id)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': f'{SOURCE_URL}?p={post_id.removeprefix("post-")}',
        'time_from': time_from,
        'venue': venue,
        'city': 'Saint-Malo',
        'country_code': 'FR',
        'description': description_for(article),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_season(session, first_url, year):
    records = []
    page_url = first_url
    visited = set()
    while page_url and page_url not in visited:
        visited.add(page_url)
        soup = get_soup(session, page_url)
        for article in soup.select('article[role="main"][id^="post-"]'):
            record = parse_article(article, year, page_url)
            if record:
                records.append(record)
        next_link = soup.select_one('a.next.page-link')
        page_url = urljoin(page_url, next_link.get('href')) if next_link else None
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for season_url in season_urls(session):
        match = re.search(r'saison-(\d{4})', season_url)
        if not match:
            continue
        try:
            records.extend(scrape_season(session, season_url, int(match.group(1))))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Classique au large season',
                event='crawler_page_failed',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda row: (
        row['date'], row['time_from'] or '', row['title'], row['venue']
    ))


class ClassiqueAuLargeFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='classiqueaularge_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ClassiqueAuLargeFrCrawler().run()


if __name__ == '__main__':
    main()
