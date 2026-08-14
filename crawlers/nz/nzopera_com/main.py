import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nzopera.com/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'opera/')
SOURCE = 'NZ Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-NZ,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|'
    'October|November|December'
)
DATE_RE = re.compile(
    rf'(?P<days>\d{{1,2}}(?:\s*(?:&|,|and)\s*\d{{1,2}})*)\s+'
    rf'(?P<month>{MONTHS})[,]?\s+(?P<year>20\d{{2}})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?:[.:](\d{2}))?\s*([ap])\.?m\.?', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).lower() == 'p' and hour != 12:
        hour += 12
    if match.group(3).lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_dates(value):
    dates = []
    value = re.sub(r'\s+', ' ', value or '')
    # WordPress editors sometimes wrap only part of a year in <strong>, which
    # inserts whitespace when the visible HTML is converted to text.
    value = re.sub(r'(?<=\d)\s+(?=\d)', '', value)
    for match in DATE_RE.finditer(value):
        month = match.group('month')
        year = match.group('year')
        for day in re.findall(r'\d{1,2}', match.group('days')):
            try:
                parsed = datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date()
            except ValueError:
                continue
            dates.append(parsed.isoformat())
    return dates


def archive_urls(session):
    page_url = ARCHIVE_URL
    seen_pages = set()
    detail_urls = []
    seen_details = set()
    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        soup = get_soup(session, page_url)
        for link in soup.select('article.type-opera h2.entry-title a[href]'):
            url = urljoin(SOURCE_URL, link.get('href'))
            if url not in seen_details:
                seen_details.add(url)
                detail_urls.append(url)
        next_link = soup.select_one('a.next.page-numbers[href]')
        page_url = urljoin(SOURCE_URL, next_link.get('href')) if next_link else None
    return detail_urls


def sidebar_groups(sidebar):
    groups = []
    group = []
    for child in sidebar.children:
        if not getattr(child, 'name', None):
            continue
        classes = child.get('class', [])
        if 'opera_sidebar_divider' in classes:
            if group:
                groups.append(group)
                group = []
            continue
        group.append(child)
    if group:
        groups.append(group)
    return groups


def parse_group(group):
    nodes = [node for root in group for node in root.select('p')]
    # Include a paragraph when it is itself a direct child in this group.
    nodes.extend(root for root in group if root.name == 'p')
    nodes = list(dict.fromkeys(nodes))
    city_node = next(
        (node for node in nodes if 'opera_sidebar_city' in node.get('class', [])),
        None,
    )
    city = clean_text(city_node) if city_node else ''
    city = re.sub(r'^(?:Tāmaki Makaurau[, ]+|Ōtautahi[, ]+|Te Whanganui-a-Tara[, ]+)', '', city, flags=re.I)
    city = city.strip(' ,')
    city = {
        'auckland': 'Auckland',
        'christchurch': 'Christchurch',
        'dunedin': 'Dunedin',
        'kerikeri': 'Kerikeri',
        'wellington': 'Wellington',
    }.get(city.lower(), city)

    venue = ''
    for node in nodes:
        text = clean_text(node)
        classes = node.get('class', [])
        if (
            'opera_sidebar_title' in classes
            and not parse_dates(text)
            and not TIME_RE.search(text)
        ):
            venue = text

    occurrences = []
    for index, node in enumerate(nodes):
        dates = parse_dates(clean_text(node))
        if not dates:
            continue
        time_from = None
        for following in nodes[index + 1:index + 3]:
            following_text = clean_text(following)
            if parse_dates(following_text):
                break
            time_from = parse_time(following_text)
            if time_from:
                break
        occurrences.extend((event_date, time_from) for event_date in dates)
    return city, venue, occurrences


def parse_detail(soup, url):
    title_node = soup.select_one('article.type-opera h1.opera_h1, article.type-opera h1')
    sidebar = soup.select_one('article.type-opera .opera_sidebar_container')
    content = soup.select_one('article.type-opera .opera_content_main')
    title = clean_text(title_node)
    description = clean_text(content)
    if not title or not sidebar:
        return []

    records = []
    for group in sidebar_groups(sidebar):
        city, venue, occurrences = parse_group(group)
        if not city or not venue:
            continue
        for event_date, time_from in occurrences:
            # Validate once more at the record boundary.
            try:
                event_date = date.fromisoformat(event_date).isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'NZ',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_detail(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    return parse_detail(get_soup(session, url), url)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = archive_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_detail, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape opera detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class NzoperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nzopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NZ',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NzoperaComCrawler().run()


if __name__ == '__main__':
    main()
