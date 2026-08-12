import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teessidemusicsociety.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
PRIOR_SEASONS_URL = urljoin(SOURCE_URL, 'copy-of-past-seasons')
SOURCE = 'Teesside Music Society'
DEFAULT_VENUE = 'Stokesley Methodist Church'
DEFAULT_CITY = 'Stokesley'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b(?:(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|'
    rf'Sat(?:urday)?|Sun(?:day)?)\s*,?\s*)?'
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})(?:\s+(20\d{{2}}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)
SEASON_RE = re.compile(r'\b(20\d{2})\s*[/–-]\s*(?:20)?(\d{2})\b')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def season_years(text):
    match = SEASON_RE.search(text)
    if not match:
        return None
    start = int(match.group(1))
    end = int(str(start)[:2] + match.group(2))
    return start, end


def parse_datetime(text, years=None):
    match = DATE_RE.search(text)
    if not match:
        return None, None
    day, month, explicit_year = match.groups()
    if explicit_year:
        year = int(explicit_year)
    elif years:
        month_number = datetime.strptime(month, '%B').month
        year = years[0] if month_number >= 7 else years[1]
    else:
        return None, None
    try:
        date = datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return None, None

    time_match = TIME_RE.search(text)
    if not time_match:
        return date, None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return date, None
    if time_match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif time_match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return date, f'{hour:02d}:{minute:02d}'


def venue_and_city(text):
    normalised = re.sub(r'\s+', ' ', text)
    if re.search(r'Stokesley Methodist Church', normalised, re.IGNORECASE):
        return DEFAULT_VENUE, DEFAULT_CITY
    if re.search(r"S\s*t\.?\s*Bernadette'?s Church", normalised, re.IGNORECASE):
        return "St Bernadette's Church", 'Nunthorpe'
    village_hall = re.search(
        r'((?:St\.?\s+Augustine(?:\'s)?|[A-Z][\w\'’-]+)\s+Village Hall)\s*,?\s*([A-Z][\w\'’-]+)',
        normalised,
        re.IGNORECASE,
    )
    if village_hall:
        return village_hall.group(1), village_hall.group(2)
    return None, None


def make_record(title, date, time_from, venue, city, description, url):
    return {
        'title': clean_text(title),
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': clean_text(description) or None,
    }


def parse_wix_page(soup, url):
    page_text = clean_text(soup)
    years = season_years(page_text)
    records = []
    seen_dates = set()

    for section in soup.select('main section'):
        text = clean_text(section)
        date, time_from = parse_datetime(text, years)
        if not date or date in seen_dates:
            continue
        venue, city = venue_and_city(text)
        if not venue:
            continue

        headings = [clean_text(node) for node in section.select('h1, h2, h3')]
        title = next(
            (
                heading for heading in headings
                if heading
                and not DATE_RE.search(heading)
                and not SEASON_RE.search(heading)
                and heading.lower() not in {'programme', 'summary', 'find out more'}
            ),
            '',
        )
        if not title:
            continue
        seen_dates.add(date)
        records.append(make_record(title, date, time_from, venue, city, text, url))
    return records


def parse_weebly_page(soup, url):
    content = soup.select_one('#wsite-content')
    if not content:
        return []
    years = season_years(clean_text(soup.select_one('title')) + ' ' + clean_text(content))
    if not years:
        url_season = re.search(r'/(20\d{2})(\d{2})-season\.html$', url)
        if url_season:
            years = int(url_season.group(1)), int(url_season.group(1)[:2] + url_season.group(2))
    records = []

    for node in content.select('h2, .paragraph'):
        text = clean_text(node)
        date, time_from = parse_datetime(text, years)
        if not date:
            continue
        venue, city = venue_and_city(text)
        if not venue:
            continue
        date_match = DATE_RE.search(text)
        prefix = text[:date_match.start()].strip(' \n,-')
        venue_match = re.search(re.escape(venue), prefix, re.IGNORECASE)
        first_line = text.split('\n', 1)[0].strip(' ,-')
        title = ''
        if first_line and not DATE_RE.search(first_line) and not venue_and_city(first_line)[0]:
            title = first_line
        elif venue_match:
            title = prefix[:venue_match.start()].strip(' \n,-')
        if not title:
            suffix = text[date_match.end():].strip(' \n,-')
            suffix = TIME_RE.sub('', suffix, count=1).strip(' \n,-')
            suffix = re.sub(r'^at\b', '', suffix, flags=re.IGNORECASE).strip(' \n,-')
            suffix = re.sub(re.escape(venue), '', suffix, count=1, flags=re.IGNORECASE).strip(' \n,-')
            title = suffix.split('\n', 1)[0].strip()
        if not title:
            previous = node.find_previous(['h2', 'div'])
            previous_text = clean_text(previous)
            if previous_text and not DATE_RE.search(previous_text):
                title = previous_text.split('\n')[-1].strip()
        title = re.sub(r'\s*-\s*S$', '', title).strip()
        if not title:
            continue
        records.append(make_record(title, date, time_from, venue, city, text, url))
    return records


def discover_pages(session):
    current = get_soup(session, CONCERTS_URL)
    wix_urls = [CONCERTS_URL]
    for link in current.select('a[href]'):
        url = urljoin(CONCERTS_URL, link['href']).split('#', 1)[0]
        if re.fullmatch(r'https://www\.teessidemusicsociety\.org/copy-of-concerts(?:-\d+)?', url):
            wix_urls.append(url)

    prior = get_soup(session, PRIOR_SEASONS_URL)
    old_archive_url = next(
        (
            link['href'] for link in prior.select('a[href]')
            if 'teessidemusicsociety.weebly.com/archive' in link['href']
        ),
        None,
    )
    weebly_urls = []
    if old_archive_url:
        archive = get_soup(session, old_archive_url)
        for link in archive.select('a[href]'):
            url = urljoin(old_archive_url, link['href'])
            if re.search(r'/20\d{4}-season\.html$', url):
                weebly_urls.append(url)
    return list(dict.fromkeys(wix_urls)), list(dict.fromkeys(weebly_urls))


class TeessideMusicSocietyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teessidemusicsociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        wix_urls, weebly_urls = discover_pages(session)
        records = []
        for url, parser in [
            *((url, parse_wix_page) for url in wix_urls),
            *((url, parse_weebly_page) for url in weebly_urls),
        ]:
            try:
                records.extend(parser(get_soup(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Teesside Music Society season',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    TeessideMusicSocietyCrawler().run()


if __name__ == '__main__':
    main()
