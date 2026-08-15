import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ccoithaca.org/'
SOURCE = 'Cayuga Chamber Orchestra'
FAMILY_URL = urljoin(SOURCE_URL, 'family-series')
CITY = 'Ithaca'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+at\s+'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.I,
)
FAMILY_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})\s*\n\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)\s*\n\s*'
    r'(Tompkins County\s+Public Library)',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    for pattern in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_time(value):
    value = re.sub(r'\s+', ' ', value.strip().upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_url(soup):
    links = []
    for link in soup.find_all('a', href=True):
        href = urljoin(SOURCE_URL, link['href'])
        if re.fullmatch(r'https://www\.ccoithaca\.org/\d{4}-\d{2}-season/?', href):
            links.append(href.rstrip('/'))
    return max(links, default=None)


def detail_urls(soup):
    urls = []
    for link in soup.find_all('a', href=True):
        if not clean_text(link.get_text(' ', strip=True)).lower().startswith('explore'):
            continue
        url = urljoin(SOURCE_URL, link['href']).rstrip('/')
        if urlparse(url).netloc == 'www.ccoithaca.org' and url not in urls:
            urls.append(url)
    return urls


def page_lines(soup):
    main = soup.find('main') or soup.body or soup
    return [clean_text(line) for line in main.get_text('\n', strip=True).splitlines() if clean_text(line)]


def parse_detail(soup, url):
    lines = page_lines(soup)
    joined = '\n'.join(lines)
    match = DATE_TIME_RE.search(joined)
    if not match:
        return None

    event_date = parse_date(match.group(1))
    time_from = parse_time(match.group(2))
    title_node = soup.find('h1')
    title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
    if not title:
        title = clean_text((soup.title.string if soup.title else '').split('|')[0])

    date_line_index = next((i for i, line in enumerate(lines) if DATE_TIME_RE.search(line)), None)
    venue = ''
    if date_line_index is not None:
        for line in lines[date_line_index + 1:date_line_index + 5]:
            if not re.search(r'pre-concert|subscribe|buy tickets|^[\W_]+$', line, re.I):
                venue = line
                break

    if not title or not event_date or not venue:
        return None

    description_lines = [line for line in lines if line not in {
        'SUBSCRIBE', 'BUY TICKETS', 'Contact Us', SOURCE,
    }]
    description = '\n'.join(description_lines)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_family(soup):
    text = '\n'.join(page_lines(soup))
    records = []
    for match in FAMILY_RE.finditer(text):
        event_date = parse_date(match.group(1))
        if not event_date:
            continue
        records.append({
            'title': 'Family Concert & Storytime',
            'date': event_date,
            'url': FAMILY_URL,
            'time_from': parse_time(match.group(2)),
            'venue': re.sub(r'\s+', ' ', match.group(3)).strip(),
            'city': CITY,
            'country_code': 'US',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    home = get_soup(session, SOURCE_URL)
    current_season_url = season_url(home)
    if not current_season_url:
        log_message('Season page not found', event='crawler_empty_listing', level='warning', url=SOURCE_URL)
        return []

    season = get_soup(session, current_season_url)
    urls = detail_urls(season)
    records = []
    for url in urls:
        try:
            record = parse_detail(get_soup(session, url), url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    try:
        records.extend(parse_family(get_soup(session, FAMILY_URL)))
    except requests.RequestException as error:
        log_message(
            'Family series request failed',
            event='crawler_detail_failed',
            level='warning',
            url=FAMILY_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    unique = {(item['title'], item['date'], item['time_from'], item['venue']): item for item in records}
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CcoIthacaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ccoithaca_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CcoIthacaOrgCrawler().run()


if __name__ == '__main__':
    main()
