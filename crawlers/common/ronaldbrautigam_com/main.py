import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ronaldbrautigam.com/'
CONCERTS_URL = f'{SOURCE_URL}concerts.php'
SOURCE = 'Ronald Brautigam'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRIES_BY_CITY = {
    'ALKMAAR': 'NL',
    'AMSTERDAM': 'NL',
    'DETMOLD': 'DE',
    'HEIDELBERG': 'DE',
    'LONDON': 'GB',
    'MEERSBURG': 'DE',
    'NEUSS': 'DE',
    'OLDEBERKOOP': 'NL',
    'REEUWIJK': 'NL',
    'SCHIERMONNIKOOG': 'NL',
    'ZÜRICH': 'CH',
}


def cell_lines(cell):
    return [text.strip() for text in cell.stripped_strings if text.strip()]


def parse_date_and_time(cell):
    text = ' '.join(cell_lines(cell))
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
        r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+'
        r'(\d{1,2}:\d{2}\s+[AP]M)',
        text,
        re.I,
    )
    if not match:
        return None, None
    try:
        date_value = datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
        time_value = datetime.strptime(match.group(2).upper(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None, None
    return date_value, time_value


def parse_row(row):
    cells = row.find_all('td', recursive=False)
    if len(cells) != 4:
        return None

    date_value, time_from = parse_date_and_time(cells[0])
    location = cell_lines(cells[1])
    programme = cell_lines(cells[2])
    performers = cell_lines(cells[3])
    if not date_value or len(location) < 2 or not programme:
        return None

    city_key = location[0].upper()
    country_code = COUNTRIES_BY_CITY.get(city_key)
    venue = location[1]
    if not country_code or not venue:
        return None

    title = ' — '.join(programme)
    link = cells[1].find('a', href=True)
    url = link['href'].strip() if link else CONCERTS_URL
    description_parts = [f'Programme: {title}']
    if performers:
        description_parts.append(f'Instrument and performers: {"; ".join(performers)}')

    return {
        'title': title,
        'date': date_value,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': location[0].title(),
        'country_code': country_code,
        'description': '\n'.join(description_parts),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RonaldBrautigamComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ronaldbrautigam_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'city', 'venue', 'title'],
    )

    def scrape(self):
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.select_one('table.concerttable')
        if table is None:
            raise ValueError('Concert table was not found')

        records = []
        skipped_count = 0
        for row in table.find_all('tr'):
            if row.get('class') and 'concertyearmonthtr' in row.get('class'):
                continue
            record = parse_row(row)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        if skipped_count:
            log_message(
                'Skipped concert rows with incomplete or unknown geography',
                event='crawler_items_skipped',
                level='warning',
                url=CONCERTS_URL,
                record_count=skipped_count,
            )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    RonaldBrautigamComCrawler().run()


if __name__ == '__main__':
    main()
