import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tso.no/'
SOURCE = 'Trondheim Symfoniorkester & Opera'
API_URL = 'https://tso.no/actions/tso/consert/get-conserts'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}
MONTHS = {
    'januar': 1, 'februar': 2, 'mars': 3, 'april': 4, 'mai': 5, 'juni': 6,
    'juli': 7, 'august': 8, 'september': 9, 'oktober': 10,
    'november': 11, 'desember': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'des': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.fullmatch(r'\s*(\d{1,2})\.?\s+([A-Za-zæøåÆØÅ]+)\.?\s+(\d{4})\s*', value or '')
    if not match:
        return None
    month = MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return datetime(int(match.group(3)), month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def infer_city(venue):
    """Resolve the site's venue labels without assigning Trondheim to tours."""
    folded = clean_text(venue).casefold()
    city_markers = (
        ('stadsbygd', 'Stadsbygd'), ('steinkjer', 'Steinkjer'),
        ('stjørdal', 'Stjørdal'), ('levanger', 'Levanger'), ('støren', 'Støren'),
        ('glåmos', 'Glåmos'), ('skaun', 'Skaun'), ('børsa', 'Børsa'),
        ('molde', 'Molde'), ('overhalla', 'Overhalla'), ('namsos', 'Namsos'),
        ('frøya', 'Sistranda'), ('oppdal', 'Oppdal'), ('grong', 'Grong'),
        ('ørland', 'Brekstad'), ('orkland', 'Orkanger'), ('den norske opera', 'Oslo'),
    )
    for marker, city in city_markers:
        if marker in folded:
            return city
    # All remaining named venues in this institutional calendar are documented
    # Trondheim venues; touring entries identify their destination in the label.
    return 'Trondheim' if folded else None


def detail_data(url):
    response = requests.get(url, headers=HEADERS, timeout=35)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    times = {}
    for occurrence in soup.select('.consert__info-date'):
        date_node = occurrence.select_one('.consert__info-date-date')
        time_node = occurrence.select_one('.consert__info-date-time')
        event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
        match = re.search(r'\b(\d{1,2}):([0-5]\d)\b', time_node.get_text(' ', strip=True) if time_node else '')
        if event_date and match and int(match.group(1)) < 24:
            times[event_date] = f'{int(match.group(1)):02d}:{match.group(2)}'

    parts = []
    lead = soup.select_one('.consert__info-ingress, .consert__info-intro')
    if lead:
        parts.append(lead.get_text('\n', strip=True))
    program = soup.select_one('.consert__program')
    if program:
        parts.append(program.get_text('\n', strip=True))
    for block in soup.select('section.block.text'):
        parts.append(block.get_text('\n', strip=True))
    description = clean_text('\n\n'.join(dict.fromkeys(clean_text(part) for part in parts if clean_text(part))))
    return times, description or None


class TsoNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tso_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _feed(self, session, archive=False):
        response = session.get(
            API_URL,
            params={'archive': 'true'} if archive else None,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return [item for month in payload.values() for item in month]

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = self._feed(session) + self._feed(session, archive=True)
        items_by_url = {
            item.get('u'): item
            for item in items
            if item.get('u') and item.get('p') and parse_date(item.get('d'))
        }

        details = {}
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(detail_data, url): url for url in items_by_url}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    details[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'TSO detail page request failed',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for item in items:
            title = clean_text(item.get('t'))
            event_date = parse_date(item.get('d'))
            url = item.get('u')
            venue = clean_text(item.get('p'))
            city = infer_city(venue)
            if not title or not event_date or not url or not venue or not city:
                continue
            times, description = details.get(url, ({}, None))
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': times.get(event_date),
                'venue': venue,
                'city': city,
                'description': description,
            })

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    return TsoNoCrawler().run()


if __name__ == '__main__':
    main()
