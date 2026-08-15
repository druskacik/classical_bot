import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tnsj.pt/pt/'
AGENDA_URL = 'https://www.tnsj.pt/pt/agenda/'
SESSIONS_URL = 'https://www.tnsj.pt/include/ajax_functions.php'
SOURCE = 'Teatro Nacional São João'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}
DATE_PATTERN = re.compile(r'\b20\d{2}-\d{2}-\d{2}\b')
TIME_PATTERN = re.compile(r'\b([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)\b', re.IGNORECASE)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def infer_city(venue):
    normalized = venue.casefold()
    cities = (
        ('paços de ferreira', 'Paços de Ferreira'),
        ('vale do sousa', 'Paços de Ferreira'),
        ('lisboa', 'Lisboa'),
        ('coimbra', 'Coimbra'),
        ('serralves', 'Porto'),
        ('palácio de cristal', 'Porto'),
        ('coliseu', 'Porto'),
        ('esmae', 'Porto'),
        ('esap', 'Porto'),
        ('lusófona', 'Porto'),
        ('balleteatro', 'Porto'),
        ('ace escola de artes', 'Porto'),
    )
    for marker, city in cities:
        if marker in normalized:
            return city

    # The TNSJ's own three buildings and otherwise unqualified locations on
    # this institutional calendar are in Porto. Explicit touring locations
    # are handled above; unknown qualified destinations are skipped.
    home_markers = ('são joão', 'carlos alberto', 'são bento da vitória')
    if any(marker in normalized for marker in home_markers):
        return 'Porto'
    return None


def extract_dates(soup):
    calendar = soup.select_one('.info-sessoes script')
    if calendar is None:
        return []
    dates = []
    for value in DATE_PATTERN.findall(calendar.get_text(' ', strip=True)):
        try:
            dates.append(date.fromisoformat(value).isoformat())
        except ValueError:
            continue
    return list(dict.fromkeys(dates))


def extract_times(session, event_id, event_date, referer):
    try:
        response = session.get(
            SESSIONS_URL,
            params={
                'action': 'getSessoes',
                'langid': 1,
                'data': event_date,
                'id': event_id,
            },
            headers={'Referer': referer, 'X-Requested-With': 'XMLHttpRequest'},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch TNSJ event sessions',
            event='crawler_fetch_failed',
            level='warning',
            url=referer,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return [None]

    times = [f'{int(hour):02d}:{minute}' for hour, minute in TIME_PATTERN.findall(response.text)]
    return list(dict.fromkeys(times)) or [None]


def parse_detail(session, html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    venue = clean_text(soup.select_one('p.local'))
    city = infer_city(venue) if venue else None
    dates = extract_dates(soup)

    if not title or not venue or not city or not dates:
        log_message(
            'Skipping TNSJ event with missing required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(dates),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return []

    description = clean_text(soup.select_one('[itemprop="description"]')) or None
    event_id_match = re.search(r'/espetaculos/(\d+)/', url)
    if not event_id_match:
        return []
    event_id = event_id_match.group(1)

    records = []
    for event_date in dates:
        for event_time in extract_times(session, event_id, event_date, url):
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': event_time,
                'venue': venue,
                'city': city,
                'country_code': 'PT',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class TnsjPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tnsj_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
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
        session = build_session()
        response = session.get(AGENDA_URL, timeout=45)
        response.raise_for_status()
        agenda = BeautifulSoup(response.text, 'html.parser')
        event_urls = list(dict.fromkeys(
            urljoin(SOURCE_URL, link['href'])
            for link in agenda.select('a[href*="/pt/espetaculos/"]')
        ))
        if not event_urls:
            raise ValueError('No event detail URLs found on the TNSJ agenda')

        def fetch_detail(url):
            detail_session = build_session()
            try:
                detail_response = detail_session.get(url, timeout=45)
                detail_response.raise_for_status()
                if '<title>Just a moment...</title>' in detail_response.text:
                    raise requests.RequestException('Cloudflare challenge page returned')
                return parse_detail(detail_session, detail_response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch TNSJ event detail',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

        with ThreadPoolExecutor(max_workers=3) as executor:
            nested_records = executor.map(fetch_detail, event_urls)
            records = [record for event_records in nested_records for record in event_records]

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    TnsjPtCrawler().run()


if __name__ == '__main__':
    main()
