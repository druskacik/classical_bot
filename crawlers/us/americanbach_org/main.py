import re
from datetime import date
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://americanbach.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar.html')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'American Bach Soloists'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month.lower(): number for number, month in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'))
        if month
}

OCCURRENCE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?[ ,\u00a0]+'
    r'(?P<year>20\d{2})\s+(?:at\s+)?'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*'
    r'(?P<ampm>[ap])\.?m\.?',
    re.IGNORECASE,
)


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    # Several pages omit an HTTP charset even though the documents are UTF-8.
    response.encoding = 'utf-8'
    return response


def valid_date(year, month_name, day):
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def time_24_hour(match):
    hour = int(match.group('hour'))
    minute = int(match.group('minute') or 0)
    if match.group('ampm').lower() == 'p' and hour != 12:
        hour += 12
    elif match.group('ampm').lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def event_title(soup):
    for selector in (
        'h1.mbr-section-title', 'h2.mbr-section-title',
        'h1.card-title', 'h1',
    ):
        for heading in soup.select(selector):
            title = clean_text(heading)
            if title and not re.search(r'\b(19|20)\d{2}\b', title):
                return title
    title = clean_text(soup.title)
    return re.sub(r'\s*[•|]\s*American Bach.*$', '', title).strip()


def description_text(soup):
    parts = []
    for section in soup.select('body > section:not(.menu):not(.footer), main'):
        if 'cookie' in ' '.join(section.get('class', [])).lower():
            continue
        text = clean_text(section, separator='\n')
        if text:
            parts.append(text)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def venue_and_city(text_after_date):
    line = re.split(r'[\r\n]+|(?:\s{2,})', text_after_date.strip(), maxsplit=1)[0]
    line = re.sub(r'^(?:at\s+)', '', line, flags=re.IGNORECASE).strip(' ,-')
    # Address components are deliberately discarded. The first component is
    # the venue and the final component is the city.
    components = [part.strip() for part in line.split(',') if part.strip()]
    if len(components) < 2:
        return None, None
    venue = components[0]
    city = components[-1]
    if re.search(r'\d', venue) or re.search(r'\d', city):
        return None, None
    return venue, city


def occurrence_records(soup, url):
    title = event_title(soup)
    description = description_text(soup)
    if not title:
        return []
    records = []
    # Exact occurrence data is presented in paragraphs, one date followed by
    # its venue/address line. Newlines are retained to separate those fields.
    for element in soup.select('p, h3.mbr-section-subtitle'):
        text = element.get_text('\n', strip=True).replace('\xa0', ' ')
        matches = list(OCCURRENCE_RE.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            venue, city = venue_and_city(text[match.end():end])
            event_date = valid_date(match.group('year'), match.group('month'), match.group('day'))
            if not event_date or not venue or not city:
                continue
            event_url = url
            section = element.find_parent('section')
            if section:
                for link in section.select('a[href]'):
                    candidate = urljoin(url, link.get('href', '')).split('#', 1)[0]
                    if (
                        urlparse(candidate).netloc == 'americanbach.org'
                        and candidate.endswith(('.html', '.htm'))
                        and candidate != url
                    ):
                        event_url = candidate
                        break
            records.append({
                'title': title,
                'date': event_date,
                'url': event_url,
                'time_from': time_24_hour(match),
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def sitemap_urls(session):
    try:
        root = ElementTree.fromstring(get_response(session, SITEMAP_URL).content)
    except (requests.RequestException, ElementTree.ParseError) as error:
        log_message(
            'Failed to inspect American Bach sitemap',
            event='crawler_index_failed',
            level='warning',
            url=SITEMAP_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []
    return [node.text.strip() for node in root.findall('.//{*}loc') if node.text]


def same_site_html_links(soup, page_url):
    links = set()
    for link in soup.select('a[href]'):
        url = urljoin(page_url, link.get('href', '')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == 'americanbach.org' and (
            parsed.path.endswith(('.html', '.htm')) or parsed.path.endswith('/')
        ):
            links.add(url)
    return links


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    calendar_soup = BeautifulSoup(get_response(session, CALENDAR_URL).text, 'html.parser')

    # The live calendar is authoritative for the current season. The sitemap
    # is old but still advertises archive calendars, so follow links from those
    # pages as well to retain scrapeable past performances.
    detail_urls = same_site_html_links(calendar_soup, CALENDAR_URL)
    archive_urls = [
        url for url in sitemap_urls(session)
        if re.search(r'(events|concerts|performances)', url, re.IGNORECASE)
    ]
    for archive_url in archive_urls:
        try:
            soup = BeautifulSoup(get_response(session, archive_url).text, 'html.parser')
            detail_urls.update(same_site_html_links(soup, archive_url))
        except requests.RequestException as error:
            log_message(
                'Failed to inspect American Bach archive page',
                event='crawler_index_failed',
                level='warning',
                url=archive_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records = occurrence_records(calendar_soup, CALENDAR_URL)
    for url in sorted(detail_urls):
        if url in {CALENDAR_URL, SOURCE_URL}:
            continue
        try:
            soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
            records.extend(occurrence_records(soup, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape American Bach event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['title']),
    )


class AmericanBachOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='americanbach_org',
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
        return get_concerts()


def main():
    AmericanBachOrgCrawler().run()


if __name__ == '__main__':
    main()
