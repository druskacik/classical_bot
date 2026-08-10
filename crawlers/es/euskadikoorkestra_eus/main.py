import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.euskadikoorkestra.eus/'
LISTING_URL = urljoin(SOURCE_URL, 'kontzertuak-eta-sarrerak/all/')
SOURCE = 'Euskadiko Orkestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'eu-ES,eu;q=0.9,es;q=0.8',
}

MONTHS = {
    'urtarrila': 1, 'otsaila': 2, 'martxoa': 3, 'apirila': 4,
    'maiatza': 5, 'ekaina': 6, 'uztaila': 7, 'abuztua': 8,
    'iraila': 9, 'urria': 10, 'azaroa': 11, 'abendua': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_urls(session):
    """Follow the public 'all season' view, which includes retained past dates."""
    url = LISTING_URL
    visited_pages = set()
    urls = set()
    while url and url not in visited_pages:
        visited_pages.add(url)
        soup = get_soup(session, url)
        view = soup.select_one('.view-listado-de-conciertos.view-display-id-page_1')
        if not view:
            break
        for link in view.select('.concierto a.fichaConcierto[href]'):
            detail_url = urljoin(SOURCE_URL, link['href'])
            if '/kontzertua/' in detail_url:
                urls.add(detail_url)
        next_link = view.select_one('.pager-next a[href]')
        url = urljoin(SOURCE_URL, next_link['href']) if next_link else None
    return sorted(urls)


def description_from_detail(soup):
    parts = []
    programme = clean_text(soup.select_one('#miniPrograma'))
    if programme:
        parts.append('Programa\n' + programme)
    description = clean_text(soup.select_one('#descripcionTxt'))
    if description:
        parts.append(description)
    return '\n\n'.join(parts) or None


def normalize_city(value):
    city = clean_text(value)
    # Subscription labels A/B are appended to Donostia, but are not city names.
    city = re.sub(r'\s+[AB]$', '', city).strip()
    return city


def normalize_venue(value):
    venue = clean_text(value)
    # Basque for "no ticket sales" is presentation text, not part of the hall.
    venue = re.sub(r'\s*\(Salmentarik ez\)\s*$', '', venue, flags=re.IGNORECASE)
    return venue.strip()


def performance_start(performance):
    calendar_link = performance.select_one('a.ics[data-fecha-init]')
    start = calendar_link.get('data-fecha-init', '') if calendar_link else ''
    match = re.fullmatch(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})', start.strip())
    if match:
        return tuple(int(match.group(index)) for index in range(1, 6))

    # Past performances remain published, but the site removes their calendar
    # link. Their visible date and time are still complete and authoritative.
    day_text = clean_text(performance.select_one('.meta > .dia'))
    date_text = clean_text(performance.select_one('.fechaHoraWrapper > .fecha')).lower()
    time_text = clean_text(performance.select_one('.fechaHoraWrapper > .hora'))
    date_match = re.search(r'([a-z]+),\s*(\d{4})', date_text)
    time_match = re.search(r'(\d{1,2}):(\d{2})', time_text)
    if not day_text.isdigit() or not date_match or not time_match:
        return None
    month = MONTHS.get(date_match.group(1))
    if not month:
        return None
    return (
        int(date_match.group(2)), month, int(day_text),
        int(time_match.group(1)), int(time_match.group(2)),
    )


def records_from_detail(soup, url):
    title = clean_text(soup.select_one('main h1'))
    description = description_from_detail(soup)
    records = []
    for performance in soup.select('#fechas > .fecha'):
        start = performance_start(performance)
        city = normalize_city(performance.select_one('.ciudad > strong'))
        venue = normalize_venue(performance.select_one('.ciudad > .lugar'))
        if not title or not start or not city or not venue:
            continue
        try:
            event_date = date(start[0], start[1], start[2]).isoformat()
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': f'{start[3]:02d}:{start[4]:02d}',
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(records_from_detail(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'],
            record['city'], record['venue'],
        ),
    )


class EuskadikoOrkestraEusCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='euskadikoorkestra_eus',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    EuskadikoOrkestraEusCrawler().run()


if __name__ == '__main__':
    main()
