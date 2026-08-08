import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lakeshorechambermusic.org/'
SOURCE = 'Lakeshore Chamber Music Society'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(value):
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'([A-Za-z]+\s+\d{1,2},\s+20\d{2})'
        r'(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?)?',
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    if not match.group(2):
        return event_date, None
    hour = int(match.group(2)) % 12
    if match.group(4).lower() == 'p':
        hour += 12
    return event_date, f'{hour:02d}:{int(match.group(3) or 0):02d}'


def parse_concert(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    title = clean_text(main.select_one('h1')) if main else ''
    concert = main.select_one('.concert') if main else None
    content = concert.select_one('.content') if concert else None
    event_date, time_from = parse_date_time(clean_text(content))
    if not title or not event_date:
        return None

    lines = [line.strip() for line in clean_text(content).splitlines() if line.strip()]
    date_line_index = next(
        (index for index, line in enumerate(lines) if parse_date_time(line)[0]),
        None,
    )
    venue = None
    city = None
    if date_line_index is not None and len(lines) > date_line_index + 2:
        venue = lines[date_line_index + 1].split(',', 1)[0].strip()
        city = lines[date_line_index + 2].split(',', 1)[0].strip()
    if not venue or not city:
        return None

    description = None
    for heading in main.select('h2'):
        if clean_text(heading).lower() == 'programme':
            description = clean_text(heading.find_parent('section')) or None
            break

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CA',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LakeshoreChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lakeshorechambermusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            archive_url = urljoin(SOURCE_URL, 'past-seasons.html')
            archive_response = session.get(archive_url, timeout=45)
            archive_response.raise_for_status()

            catalogue_urls = [SOURCE_URL]
            archive_soup = BeautifulSoup(archive_response.text, 'html.parser')
            catalogue_urls.extend(
                urljoin(archive_url, link['href'])
                for link in archive_soup.select('a[href*="season-"]')
            )

            concert_urls = set()
            for catalogue_url in dict.fromkeys(catalogue_urls):
                if catalogue_url == SOURCE_URL:
                    html = home_response.text
                else:
                    response = session.get(catalogue_url, timeout=45)
                    response.raise_for_status()
                    html = response.text
                soup = BeautifulSoup(html, 'html.parser')
                concert_urls.update(
                    urljoin(catalogue_url, link['href'])
                    for link in soup.select('a[href*="/concerts/en/concert-"]')
                )

            records = []
            for concert_url in sorted(concert_urls):
                response = session.get(concert_url, timeout=45)
                response.raise_for_status()
                record = parse_concert(response.text, concert_url)
                if record:
                    records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Lakeshore Chamber Music catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    LakeshoreChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
