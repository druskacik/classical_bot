import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mariotticonductor.com/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
SOURCE = 'Michele Mariotti'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}

# The artist tours internationally. These are the cities used by the agenda,
# including venue names which omit their city in the visible text.
VENUE_GEOGRAPHY = {
    'wiener staatsoper': ('Vienna', 'AT'),
    'orvieto cathedral': ('Orvieto', 'IT'),
    'teatro dell’opera di roma': ('Rome', 'IT'),
    "teatro dell'opera di roma": ('Rome', 'IT'),
    'royal concertgebouw amsterdam': ('Amsterdam', 'NL'),
    'maggio musicale fiorentino': ('Florence', 'IT'),
    'caracalla baths, caracalla festival': ('Rome', 'IT'),
    'opéra national de paris': ('Paris', 'FR'),
}

CITY_COUNTRIES = {
    'Amsterdam': 'NL',
    'Bologna': 'IT',
    'Bolzano': 'IT',
    'Bozen': 'IT',
    'Bucharest': 'RO',
    'Cremona': 'IT',
    'Dallas': 'US',
    'Ferrara': 'IT',
    'Florence': 'IT',
    'Kawasaki': 'JP',
    'London': 'GB',
    'Lugano': 'CH',
    'Milan': 'IT',
    'Naples': 'IT',
    'New York City': 'US',
    'Paris': 'FR',
    'Reggio Emilia': 'IT',
    'Rome': 'IT',
    'Sion': 'CH',
    'Stuttgart': 'DE',
    'Toblach': 'IT',
    'Tokyo': 'JP',
    'Trento': 'IT',
    'Turin': 'IT',
    'Venice': 'IT',
    'Vienna': 'AT',
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def paragraph_lines(paragraph):
    fragment = BeautifulSoup(str(paragraph), 'html.parser')
    for br in fragment.find_all('br'):
        br.replace_with('\n')
    return [clean_text(line) for line in fragment.get_text('\n').splitlines() if clean_text(line)]


def geography_for(venue):
    normalized = clean_text(venue).casefold()
    if normalized in VENUE_GEOGRAPHY:
        return VENUE_GEOGRAPHY[normalized]

    if ',' in venue:
        city = clean_text(venue.rsplit(',', 1)[1])
        country_code = CITY_COUNTRIES.get(city)
        if country_code:
            return city, country_code
    return None


def dates_from_line(value, year, month):
    month_name = next(name for name, number in MONTHS.items() if number == month)
    match = re.search(rf'^(.*?)\b{month_name}\b', clean_text(value), re.I)
    if not match:
        return []

    results = []
    for day_text in re.findall(r'\d{1,2}', match.group(1)):
        try:
            results.append(date(year, month, int(day_text)).isoformat())
        except ValueError:
            continue
    return results


def parse_agenda(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []

    for year_heading in soup.select('h4'):
        year_text = clean_text(year_heading.get_text(' ', strip=True))
        if not re.fullmatch(r'20\d{2}', year_text):
            continue
        year = int(year_text)

        node = year_heading.find_next_sibling()
        while node is not None:
            if node.name == 'h4' and re.fullmatch(
                r'20\d{2}', clean_text(node.get_text(' ', strip=True))
            ):
                break

            for item in node.select('.toggle-item') if hasattr(node, 'select') else []:
                month_heading = item.select_one('.toggle-name')
                month_name = clean_text(month_heading.get_text(' ', strip=True)) if month_heading else ''
                month = MONTHS.get(month_name)
                if not month:
                    continue

                for paragraph in item.select('.toggle-inner p'):
                    lines = paragraph_lines(paragraph)
                    if len(lines) < 2:
                        continue

                    # Inline formatting occasionally splits a date list into
                    # two text nodes (for example ``23,`` + ``24, 26 April``).
                    date_line_end = next(
                        (index for index, line in enumerate(lines) if month_name in line),
                        0,
                    )
                    date_line = ' '.join(lines[:date_line_end + 1])
                    detail_lines = lines[date_line_end + 1:]
                    if not detail_lines:
                        continue

                    event_dates = dates_from_line(date_line, year, month)
                    venue = detail_lines[0]
                    geography = geography_for(venue)
                    if not event_dates or not geography:
                        log_message(
                            'Skipped incomplete Michele Mariotti agenda item',
                            event='crawler_item_skipped',
                            level='warning',
                            url=AGENDA_URL,
                            error_type='IncompleteEventData',
                            error_message='A valid date, city, or country could not be resolved',
                        )
                        continue

                    city, country_code = geography
                    link = paragraph.find('a', href=True)
                    event_url = urljoin(AGENDA_URL, link['href']) if link else AGENDA_URL
                    description = '\n'.join(detail_lines[1:]) or None
                    title = f'Michele Mariotti at {venue}'
                    for event_date in event_dates:
                        records.append({
                            'title': title,
                            'date': event_date,
                            'url': event_url,
                            'time_from': None,
                            'venue': venue,
                            'city': city,
                            'country_code': country_code,
                            'description': description,
                        })
            node = node.find_next_sibling()

    return sorted(records, key=lambda item: (item['date'], item['venue'], item['title']))


class MariottiConductorComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mariotticonductor_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(AGENDA_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = parse_agenda(response.text)
        log_message(
            'Michele Mariotti agenda parsed',
            event='crawler_scrape_completed',
            url=AGENDA_URL,
            record_count=len(records),
        )
        return records


def main():
    MariottiConductorComCrawler().run()


if __name__ == '__main__':
    main()
