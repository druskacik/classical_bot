import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.barocchistiecoro.ch/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'I Barocchisti e Coro RSI'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-CH,it;q=0.9,de;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

CITY_COUNTRIES = {
    'Airolo': 'CH', 'Bellinzona': 'CH', 'Cavergno': 'CH', 'Chiasso': 'CH',
    'Faido': 'CH', 'Locarno': 'CH', 'Losanna': 'CH', 'Lugano': 'CH',
    'Mendrisio': 'CH', 'Mesocco': 'CH', 'Roveredo': 'CH',
    'Santa Maria in Calanca': 'CH', 'Thun': 'CH',
    'Lipsia': 'DE', 'Milano': 'IT', 'Modena': 'IT', 'Piacenza': 'IT',
    'Reggio Emilia': 'IT', 'Roma': 'IT', 'Vicenza': 'IT',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = text.replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    urls = []
    for location in soup.find_all('loc'):
        url = clean_text(location)
        if urlparse(url).path.startswith('/it/concerti/'):
            urls.append(url)
    return sorted(set(urls))


def parse_dates(text):
    results = []
    pattern = re.compile(
        r'(?P<days>\d{1,2}(?:\s*(?:-|,|e)\s*\d{1,2})*)\s*'
        r'(?P<month>' + '|'.join(MONTHS) + r')\s*(?P<year>20\d{2})?',
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        month = MONTHS[match.group('month').lower()]
        year_text = match.group('year')
        for day_text in re.findall(r'\d{1,2}', match.group('days')):
            day = int(day_text)
            years = [int(year_text)] if year_text else range(date.today().year - 5, date.today().year + 2)
            candidates = []
            for year in years:
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                candidates.append(candidate)
            if not year_text:
                weekday_names = ('lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica')
                stated = next((index for index, name in enumerate(weekday_names) if name in text.lower()), None)
                if stated is not None:
                    candidates = [item for item in candidates if item.weekday() == stated]
                candidates = [item for item in candidates if item <= date.today().replace(year=date.today().year + 1)]
            if candidates:
                results.append(max(candidates).isoformat())
    return list(dict.fromkeys(results))


def locations_from_lines(lines):
    locations = []
    for index, line in enumerate(lines[:12]):
        for city, country_code in CITY_COUNTRIES.items():
            if not re.search(rf'\b{re.escape(city)}\b', line, re.IGNORECASE):
                continue
            if line.casefold() == city.casefold():
                venue_index = index - 1
                while venue_index >= 0 and (
                    re.search(r'\b\d{1,2}:\d{2}\b', lines[venue_index])
                    or parse_dates(lines[venue_index])
                    or lines[venue_index].lower() in {'concerto', 'orari diversi'}
                ):
                    venue_index -= 1
                venue = lines[venue_index] if venue_index >= 0 else ''
                if venue.lower().startswith('via ') and venue_index > 0:
                    venue = lines[venue_index - 1]
            else:
                venue = line.split(',', 1)[0].strip()
            if venue and venue.casefold() != city.casefold():
                item = (venue.rstrip('*'), city, country_code)
                if item not in locations:
                    locations.append(item)
    return locations


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    sections = soup.select('.text-rich-text.concerts')
    if not title or not sections:
        return []

    description = '\n\n'.join(filter(None, (clean_text(section) for section in sections))) or None
    lines = [line for line in clean_text(sections[0]).splitlines() if line]
    leading_text = '\n'.join(lines[:12])
    dates = parse_dates(leading_text)
    locations = locations_from_lines(lines)
    times = list(dict.fromkeys(re.findall(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b', leading_text)))
    if not dates or not locations:
        return []

    records = []
    for index, event_date in enumerate(dates):
        venue, city, country_code = locations[index] if len(locations) == len(dates) else locations[0]
        time_from = times[index] if len(times) == len(dates) else (times[0] if len(times) == 1 else None)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
        })
    return records


def fetch_and_parse(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class BarocchistiECoroChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='barocchistiecoro_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        urls = event_urls(response.text)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_and_parse, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch or parse Barocchisti concert',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    BarocchistiECoroChCrawler().run()


if __name__ == '__main__':
    main()
