import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mozarteum.org.br/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programacao-e-ingressos/')
SOURCE = 'Mozarteum Brasileiro'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'janeiro': 1,
    'fev': 2, 'fevereiro': 2,
    'mar': 3, 'marco': 3,
    'abr': 4, 'abril': 4,
    'mai': 5, 'maio': 5,
    'jun': 6, 'junho': 6,
    'jul': 7, 'julho': 7,
    'ago': 8, 'agosto': 8,
    'set': 9, 'setembro': 9,
    'out': 10, 'outubro': 10,
    'nov': 11, 'novembro': 11,
    'dez': 12, 'dezembro': 12,
}

# The site does not publish addresses or cities on its programme pages. These
# venues are unambiguously in Sao Paulo; unknown venues are skipped rather
# than receiving the organisation's home city when it might be touring.
VENUE_CITIES = {
    'sala sao paulo': 'São Paulo',
    'teatro b32': 'São Paulo',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = clean_text(value).lower()
    return value.translate(str.maketrans('áàâãéêíóôõúç', 'aaaaeeiooouc'))


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_listing_date(value):
    # A date range represents an academy or multi-day project rather than one
    # defensible concert date, so only a single complete date is accepted.
    text = normalized(value)
    match = re.fullmatch(r'(\d{1,2})\s+([a-z]+),?\s+(\d{4})', text)
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(soup):
    date_nodes = soup.select('.webdoor.w-events-detail .date, .events-presentation')
    match = re.search(
        r'\b(\d{1,2})h(\d{2})\b',
        '\n'.join(clean_text(node) for node in date_nodes),
    )
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def description_from_detail(soup):
    parts = []
    presentation = soup.select_one('.events-presentation')
    if presentation:
        text = clean_text(presentation)
        if text:
            parts.append(text)

    programme = soup.select_one('.other-infos')
    if programme:
        text = clean_text(programme)
        if text and text not in parts:
            parts.append('PROGRAMA\n' + text)
    return '\n\n'.join(parts) or None


def listing_items(soup):
    seen = set()
    for link in soup.select('a.link-intern[href*="/programacao/"]'):
        url = urljoin(PROGRAM_URL, link.get('href', ''))
        if not url or url in seen:
            continue
        seen.add(url)
        wrapper = link.find_parent(class_='wrapper')
        if wrapper:
            yield url, wrapper


def make_record(session, url, wrapper):
    title = clean_text(wrapper.select_one('.name'))
    venue = clean_text(wrapper.select_one('.location')).strip(' -')
    event_date = parse_listing_date(wrapper.select_one('.date'))
    city = VENUE_CITIES.get(normalized(venue))
    if not title or not venue or not city or not event_date:
        return None

    detail = get_soup(session, url)
    detail_title = clean_text(detail.select_one('.webdoor.w-events-detail h1'))
    return {
        'title': detail_title or title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(detail),
        'venue': venue,
        'city': city,
        'country_code': 'BR',
        'description': description_from_detail(detail),
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    listing = get_soup(session, PROGRAM_URL)
    records = []
    for url, wrapper in listing_items(listing):
        try:
            record = make_record(session, url, wrapper)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Mozarteum concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MozarteumOrgBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mozarteum_org_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MozarteumOrgBrCrawler().run()


if __name__ == '__main__':
    main()
