import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.charlottesvilleopera.org/'
SOURCE = 'Charlottesville Opera'
SEASON_URL = urljoin(SOURCE_URL, 'season.html')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

EXTRA_FEED_PAGES = ('free-summer-events.html',)
EXPLICIT_EVENT_PAGES = ('americansongbookconcert.html', 'singmeastory.html')
DATE_RE = re.compile(
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', re.I)
VENUE_CITY = {
    'The Paramount Theater': 'Charlottesville',
    'Paramount Theater': 'Charlottesville',
    'Ting Pavilion': 'Charlottesville',
    'Farmington Country Club': 'Charlottesville',
    'V. Earl Dickinson Center for the Performing Arts at PVCC': 'Charlottesville',
    'V. Earl Dickinson Theater': 'Charlottesville',
    'Louisa Arts Center': 'Louisa',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def sitemap_years(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    years = {}
    for item in soup.find_all('url'):
        location = item.find('loc')
        modified = item.find('lastmod')
        if location and modified and re.match(r'20\d{2}', modified.get_text(strip=True)):
            years[location.get_text(strip=True)] = int(modified.get_text(strip=True)[:4])
    return years


def embedded_links(html, base_url):
    links = []
    for raw in re.findall(r'["\']link["\']\s*:\s*["\']([^"\']+)', html):
        raw = re.sub(r'weeblylink_new_window$', '', raw)
        if raw and raw != '#':
            links.append(urljoin(base_url, raw))
    return links


def feed_links(session):
    links = []
    for page_url in [SEASON_URL, *(urljoin(SOURCE_URL, page) for page in EXTRA_FEED_PAGES)]:
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        links.extend(embedded_links(response.text, page_url))
        if page_url != SEASON_URL:
            soup = BeautifulSoup(response.text, 'html.parser')
            content = soup.select_one('.body-wrap') or soup
            for node in content.select('a[href]'):
                link = urljoin(page_url, node['href'])
                if 'ticketstripe.com/events/' in link or link.endswith('/festivall.html'):
                    links.append(link)
    links.extend(urljoin(SOURCE_URL, page) for page in EXPLICIT_EVENT_PAGES)
    return list(dict.fromkeys(link for link in links if link.startswith(('http://', 'https://'))))


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    parsed = datetime.strptime(f'{hour}:{minute or "00"} {meridiem}', '%I:%M %p')
    return parsed.strftime('%H:%M')


def page_title(soup):
    title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    return re.sub(r'\s+-\s+Charlottesville Opera$', '', title, flags=re.I)


def find_venue_and_city(text):
    found = []
    for venue, city in VENUE_CITY.items():
        position = text.lower().find(venue.lower())
        if position >= 0:
            found.append((position, venue, city))
    if found:
        _, venue, city = min(found)
        return venue, city
    return '', ''


def parse_event_page(url, html, fallback_year):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('.body-wrap') or soup.body or soup
    text = clean_text(content.get_text('\n', strip=True))
    venue, city = find_venue_and_city(text)
    title = page_title(soup)
    if not title or not venue or not city or re.search(r'news\s*&?\s*highlights', title, re.I):
        return []

    records = []
    matches = list(DATE_RE.finditer(text))
    for index, match in enumerate(matches):
        year = int(match.group('year') or fallback_year)
        try:
            event_date = datetime.strptime(
                f'{match.group("month")} {match.group("day")} {year}', '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        next_date_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        time_from = parse_time(text[match.end():next_date_start])
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    years = sitemap_years(session)
    records = []
    for url in feed_links(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            fallback_year = years.get(url, datetime.now().year)
            records.extend(parse_event_page(url, response.text, fallback_year))
        except requests.RequestException as error:
            log_message(
                'Event page request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No concrete performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
    return result


class CharlottesvilleOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='charlottesvilleopera_org',
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
    CharlottesvilleOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
