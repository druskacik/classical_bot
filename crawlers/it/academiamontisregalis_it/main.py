import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://academiamontisregalis.it/it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario.php')
SOURCE = 'Academia Montis Regalis'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def event_ids(soup):
    ids = []
    for link in soup.select('a[href*="calendario.php?e="]'):
        values = parse_qs(urlparse(link.get('href', '')).query).get('e', [])
        if values and values[0].isdigit() and values[0] not in ids:
            ids.append(values[0])
    return ids


def archive_ids(session):
    first_url = f'{CALENDAR_URL}?ctp=archive'
    first_soup = get_soup(session, first_url)
    page_numbers = [1]
    total_rows = None
    for link in first_soup.select('a[href*="ctp=archive"][href*="uwpi="]'):
        query = parse_qs(urlparse(link.get('href', '')).query)
        values = query.get('uwpi', [])
        if values and values[0].isdigit():
            page_numbers.append(int(values[0]))
        totals = query.get('uwtr', [])
        if totals and totals[0].isdigit():
            total_rows = int(totals[0])

    ids = []
    for page_number in range(1, max(page_numbers) + 1):
        if page_number == 1:
            soup = first_soup
        else:
            parameters = {'uwpi': page_number, 'ctp': 'archive'}
            if total_rows is not None:
                parameters['uwtr'] = total_rows
            query = urlencode(parameters)
            soup = get_soup(session, f'{CALENDAR_URL}?{query}')
        for event_id in event_ids(soup):
            if event_id not in ids:
                ids.append(event_id)
    return ids


def parse_location(value):
    parts = [part.strip() for part in value.split('|') if part.strip()]
    parts = [part for part in parts if not re.fullmatch(r'ore\s*\d{1,2}:\d{2}', part, re.I)]
    if len(parts) < 2:
        return None

    venue, city_text = parts[0], parts[1]
    if 'castel mareccio' in city_text.casefold():
        venue, city_text = 'Castel Mareccio', 'Bolzano'
    city = city_text.split(',', 1)[0].strip()
    if city.casefold() == 'innsbruch':
        return venue, 'Innsbruck', 'AT'
    if not venue or not city:
        return None
    return venue, city, 'IT'


def parse_detail(soup, url, year):
    date_nodes = soup.select('.calendar-date')
    title_node = soup.select_one('h3.el-title.uk-h2')
    location_node = title_node.find_previous_sibling(class_='el-meta') if title_node else None
    if len(date_nodes) < 2 or title_node is None or location_node is None:
        return None

    try:
        day = int(clean_text(date_nodes[0]))
        month = MONTHS[clean_text(date_nodes[1]).casefold()]
        event_date = date(year, month, day).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    title = clean_text(title_node)
    location_text = clean_text(location_node)
    location = parse_location(location_text)
    if not title or not location:
        return None

    time_match = re.search(r'\bore\s*(\d{1,2}):(\d{2})\b', location_text, re.I)
    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    for sibling in title_node.find_next_siblings():
        if sibling.name == 'a' or sibling.select_one('img'):
            continue
        text = clean_text(sibling)
        if text and text.casefold() != 'torna':
            description_parts.append(text)

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text('\n\n'.join(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_month(soup):
    nodes = soup.select('.calendar-date')
    if len(nodes) < 2:
        return None
    return MONTHS.get(clean_text(nodes[1]).casefold())


class AcademiaMontisregalisItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='academiamontisregalis_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            current_ids = event_ids(get_soup(session, CALENDAR_URL))
            past_ids = archive_ids(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Academia Montis Regalis calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        today = date.today()
        previous_month = None
        archive_year = today.year
        for event_id in current_ids + past_ids:
            url = f'{CALENDAR_URL}?e={event_id}'
            try:
                soup = get_soup(session, url)
                month = detail_month(soup)
                if event_id in past_ids:
                    if previous_month is not None and month is not None and month > previous_month:
                        archive_year -= 1
                    year = archive_year
                    if month is not None:
                        previous_month = month
                else:
                    nodes = soup.select('.calendar-date')
                    day = int(clean_text(nodes[0])) if nodes else 1
                    year = today.year + (1 if month and (month, day) < (today.month, today.day) else 0)

                record = parse_detail(soup, url, year)
                if record:
                    records.append(record)
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Academia Montis Regalis event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AcademiaMontisregalisItCrawler().run()


if __name__ == '__main__':
    main()
