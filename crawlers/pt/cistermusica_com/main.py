import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cistermusica.com/pt'
PROGRAMME_URL = f'{SOURCE_URL}/programacao'
SOURCE = 'Cistermúsica - Festival de Música de Alcobaça'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'janeiro': 1,
    'fevereiro': 2,
    'março': 3,
    'abril': 4,
    'maio': 5,
    'junho': 6,
    'julho': 7,
    'agosto': 8,
    'setembro': 9,
    'outubro': 10,
    'novembro': 11,
    'dezembro': 12,
}


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
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def infer_city(venue):
    normalized = venue.casefold()
    locations = (
        ('coimbra', 'Coimbra'),
        ('lisboa', 'Lisboa'),
        ('porto de mós', 'Porto de Mós'),
        ('paredes da vitória', 'Paredes da Vitória'),
        ('são martinho do porto', 'São Martinho do Porto'),
        ('pataias', 'Pataias'),
        ('benedita', 'Benedita'),
        ('aljubarrota', 'Aljubarrota'),
        ('famalicão', 'Famalicão'),
        ('nazaré', 'Nazaré'),
        ('cós', 'Cós'),
        ('alcobaça', 'Alcobaça'),
    )
    for marker, city in locations:
        if marker in normalized:
            return city

    # Unqualified venues in this municipal festival's programme are in its
    # home city. Touring entries explicitly name their destination above.
    return 'Alcobaça'


def parse_date(day_text, month_text, year):
    month = MONTHS.get(month_text.casefold())
    if not month:
        return None
    try:
        return date(year, month, int(day_text)).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])[h:]([0-5]\d)\b', value.casefold())
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_detail(html, url, year):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('#content h1.title'))
    day = clean_text(soup.select_one('#content .date-category .dia'))
    month = clean_text(soup.select_one('#content .date-category .mes'))
    event_date = parse_date(day, month, year)
    venue = re.sub(r'^Local:\s*', '', clean_text(soup.select_one('#content .local')), flags=re.I)

    if not title or not event_date or not venue:
        log_message(
            'Skipping Cistermúsica event with missing required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_venue=bool(venue),
        )
        return None

    description_parts = [
        clean_text(soup.select_one('#content h3.subsubtitle')),
        clean_text(soup.select_one('#content .texto')),
    ]
    description = '\n\n'.join(part for part in description_parts if part) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(soup.select_one('#content .horas'))),
        'venue': venue,
        'city': infer_city(venue),
        'country_code': 'PT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CistermusicaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cistermusica_com',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = build_session()
        try:
            response = session.get(PROGRAMME_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Cistermúsica programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAMME_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        event_urls = list(dict.fromkeys(
            link['href'] for link in soup.select('a.evento__panel[href]')
        ))
        years = re.findall(r'cistermusica(20\d{2})-', response.text, flags=re.I)
        if not event_urls or not years:
            raise ValueError('Could not find programme events or determine programme year')
        year = int(Counter(years).most_common(1)[0][0])

        def fetch_detail(url):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                return parse_detail(detail_response.text, url, year)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Cistermúsica event',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

        with ThreadPoolExecutor(max_workers=6) as executor:
            records = [record for record in executor.map(fetch_detail, event_urls) if record]

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CistermusicaComCrawler().run()


if __name__ == '__main__':
    main()
