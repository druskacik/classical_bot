import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.aarhuskammermusikfestival.dk/'
SOURCE = 'Aarhus Kammermusikfestival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1,
    'feb': 2,
    'mar': 3,
    'apr': 4,
    'maj': 5,
    'jun': 6,
    'jul': 7,
    'aug': 8,
    'sep': 9,
    'okt': 10,
    'nov': 11,
    'dec': 12,
}

# These programme entries are festival activities, but not concerts.
NON_CONCERT_TITLES = {
    'artist talk med dsq',
    'social dining',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\x00', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def find_programme_url(soup):
    candidates = []
    for link in soup.select('a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        match = re.search(r'/program-(20\d{2})(?:[/?#]|$)', url)
        if match:
            candidates.append((int(match.group(1)), url.split('#', 1)[0]))
    if not candidates:
        return None
    return max(candidates)[1]


def detail_urls(programme_soup, programme_url):
    host = urlparse(SOURCE_URL).netloc
    ignored_paths = {
        '',
        '/',
        '/artister',
        '/galleri',
        '/køb-billetter',
        '/om-os',
        urlparse(programme_url).path.rstrip('/'),
    }
    urls = []
    for link in programme_soup.select('main a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', '')).split('#', 1)[0]
        parsed = urlparse(url)
        path = parsed.path.rstrip('/') or '/'
        if parsed.netloc != host or path in ignored_paths:
            continue
        normalized = f'{parsed.scheme}://{parsed.netloc}{path}'
        if normalized not in urls:
            urls.append(normalized)
    return urls


def parse_detail(soup, url, year):
    main = soup.select_one('main')
    if main is None:
        return None

    lines = [line for line in clean_text(main).splitlines() if line.strip()]
    if not lines:
        return None
    title = lines[0]
    if title.casefold() in NON_CONCERT_TITLES:
        return None

    full_text = '\n'.join(lines)
    date_match = re.search(
        r'\bDato\s+(\d{1,2})\s+([A-Za-zÆØÅæøå]{3,})\s+'
        r'([01]?\d|2[0-3])[:.]([0-5]\d)\b',
        full_text,
        re.IGNORECASE,
    )
    room_match = re.search(r'\bSal\s+([^\n]+)', full_text, re.IGNORECASE)
    if not date_match or not room_match:
        return None

    month = MONTHS.get(date_match.group(2)[:3].lower())
    if month is None:
        return None
    try:
        event_date = date(year, month, int(date_match.group(1))).isoformat()
    except ValueError:
        return None

    room = room_match.group(1).strip()
    if not room:
        return None
    venue = f'Musikhuset Aarhus – {room}'

    # Preserve all programme/composer text while excluding the compact facts,
    # prices and ticket calls-to-action at the top of each detail page.
    description_start = 1
    price_index = next(
        (index for index, line in enumerate(lines) if line.casefold() == 'pris'),
        None,
    )
    if price_index is not None:
        description_start = min(price_index + 2, len(lines))
    description_lines = [
        line for line in lines[description_start:]
        if line.casefold() not in {'køb billet', 'program'}
    ]
    description = '\n'.join(description_lines).strip() or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{int(date_match.group(3)):02d}:{date_match.group(4)}',
        'venue': venue,
        'city': 'Aarhus',
        'country_code': 'DK',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AarhusKammermusikfestivalDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='aarhuskammermusikfestival_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SOURCE_URL, timeout=45)
            response.raise_for_status()
            programme_url = find_programme_url(BeautifulSoup(response.text, 'html.parser'))
            if programme_url is None:
                raise ValueError('Could not find a dated programme page')

            programme_response = session.get(programme_url, timeout=45)
            programme_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Aarhus Kammermusikfestival programme',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        year_match = re.search(r'/program-(20\d{2})(?:[/?#]|$)', programme_url)
        if year_match is None:
            raise ValueError(f'Programme URL has no year: {programme_url}')
        year = int(year_match.group(1))
        programme_soup = BeautifulSoup(programme_response.text, 'html.parser')

        records = []
        for url in detail_urls(programme_soup, programme_url):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch festival event detail',
                    event='crawler_item_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_detail(
                BeautifulSoup(detail_response.text, 'html.parser'), url, year
            )
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    AarhusKammermusikfestivalDkCrawler().run()


if __name__ == '__main__':
    main()
