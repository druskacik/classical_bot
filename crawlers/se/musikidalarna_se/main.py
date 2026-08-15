import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musikidalarna.se/'
SOURCE = 'Musik i Dalarna'
CALENDAR_URL = urljoin(SOURCE_URL, 'evenemang/')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1, 'februari': 2, 'mars': 3, 'april': 4,
    'maj': 5, 'juni': 6, 'juli': 7, 'augusti': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit(('https', 'musikidalarna.se', parts.path.rstrip('/') + '/', '', ''))


def split_values(value):
    return [part.strip() for part in clean_text(value).split(',') if part.strip()]


def normalise_city(value):
    value = clean_text(value)
    # The calendar occasionally gives an area as its location. Preserve that
    # useful text as the venue while keeping the municipality in the city field.
    if value.casefold() == 'borlänge centrum':
        return 'Borlänge'
    return value


def date_parts(value):
    text = clean_text(value).casefold().replace('–', '-')
    single = re.fullmatch(r'(\d{1,2})\s+([a-zåäö]+)', text)
    if single and single.group(2) in MONTHS:
        return [(int(single.group(1)), MONTHS[single.group(2)])]

    same_month = re.fullmatch(r'(\d{1,2})-(\d{1,2})\s+([a-zåäö]+)', text)
    if same_month and same_month.group(3) in MONTHS:
        first, last = int(same_month.group(1)), int(same_month.group(2))
        month = MONTHS[same_month.group(3)]
        return [(day, month) for day in range(first, last + 1)]

    cross_month = re.fullmatch(
        r'(\d{1,2})\s+([a-zåäö]+)-(\d{1,2})\s+([a-zåäö]+)', text
    )
    if cross_month and cross_month.group(2) in MONTHS and cross_month.group(4) in MONTHS:
        return [
            (int(cross_month.group(1)), MONTHS[cross_month.group(2)]),
            (int(cross_month.group(3)), MONTHS[cross_month.group(4)]),
        ]
    return []


def calendar_items(soup):
    items = []
    for node in soup.select('.pt-cv-content-item[data-pid]'):
        title_node = node.select_one('.pt-cv-title a')
        date_node = node.select_one('.pt-cv-ctf-datum_text .pt-cv-ctf-value')
        city_node = node.select_one('.pt-cv-ctf-tid_spelstalle_ort .pt-cv-ctf-value')
        if not all((title_node, date_node, city_node)):
            continue
        items.append({
            'title': clean_text(title_node),
            'url': canonical_url(title_node.get('href')),
            'date_text': clean_text(date_node),
            'cities': split_values(city_node),
        })
    return items


def detail_data(session, url, venue_count):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    content = soup.select_one('#page-content')
    description = clean_text(content)
    if description:
        description = re.split(r'\nHITTA HIT\n|\nBESÖKSINFO\n', description, maxsplit=1)[0]

    venues = []
    hit_heading = next(
        (node for node in soup.find_all(re.compile(r'^h[1-6]$'))
         if clean_text(node).casefold() == 'hitta hit'),
        None,
    )
    if hit_heading:
        section = hit_heading.find_parent(class_=lambda value: value and 'tatsu-section' in value)
        if section:
            venues = [clean_text(node) for node in section.select('h4') if clean_text(node)]
    if not venues:
        # Current templates sometimes put the label and venue headings in
        # neighbouring page-builder sections rather than one semantic section.
        headings = [clean_text(node) for node in soup.select('#page-content h4')]
        candidates = [value for value in headings if value][-venue_count:]
        labels = {
            'artist', 'dirigent', 'dirigent/solist', 'fri entré', 'medverkande',
            'repertoar', 'solist',
        }
        if candidates and not any(value.casefold() in labels for value in candidates):
            venues = candidates
    return description or None, venues


def item_records(session, item, year):
    parts = date_parts(item['date_text'])
    cities = item['cities']
    repeated_location = (
        len(cities) == 1
        and len(parts) > 1
        and re.fullmatch(
            r'\d{1,2}[-–]\d{1,2}\s+[a-zåäö]+',
            item['date_text'].casefold(),
        )
    )
    if repeated_location:
        cities = cities * len(parts)
    if not parts or len(parts) != len(cities):
        log_message(
            'Skipped event with ambiguous occurrence dates',
            event='crawler_item_skipped',
            level='warning',
            url=item['url'],
            date_text=item['date_text'],
            city_count=len(cities),
        )
        return []

    description, venues = detail_data(
        session, item['url'], 1 if repeated_location else len(cities)
    )
    if repeated_location and len(venues) == 1:
        venues = venues * len(parts)
    if len(venues) != len(cities):
        # A location such as "Borlänge centrum" is strong venue evidence in
        # the calendar itself; ordinary city names are not venue placeholders.
        if len(cities) == 1 and cities[0].casefold() == 'borlänge centrum':
            venues = [cities[0]]
        else:
            log_message(
                'Skipped event with unresolved venue mapping',
                event='crawler_item_skipped',
                level='warning',
                url=item['url'],
                venue_count=len(venues),
                city_count=len(cities),
            )
            return []

    records = []
    occurrence_year = year
    previous_month = None
    for (day, month), city_value, venue in zip(parts, cities, venues):
        if previous_month is not None and month < previous_month:
            occurrence_year += 1
        previous_month = month
        try:
            event_date = date(occurrence_year, month, day).isoformat()
        except ValueError:
            continue
        records.append({
            'title': item['title'],
            'date': event_date,
            'url': item['url'],
            'time_from': None,
            'venue': venue,
            'city': normalise_city(city_value),
            'description': description,
        })
    return records


class MusikidalarnaSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikidalarna_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CALENDAR_URL, timeout=45)
        response.raise_for_status()
        items = calendar_items(BeautifulSoup(response.content, 'html.parser'))

        today = date.today()
        year = today.year
        if items:
            first = date_parts(items[0]['date_text'])
            if first and first[0][1] < today.month - 2:
                year += 1

        records = []
        item_years = []
        first_calendar_month = None
        if items:
            first_parts = date_parts(items[0]['date_text'])
            first_calendar_month = first_parts[0][1] if first_parts else None
        for item in items:
            parts = date_parts(item['date_text'])
            first_month = parts[0][1] if parts else first_calendar_month
            item_years.append(
                year + int(
                    first_month is not None
                    and first_calendar_month is not None
                    and first_month < first_calendar_month
                )
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(item_records, session, item, item_year): item['url']
                for item, item_year in zip(items, item_years)
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Musik i Dalarna event detail',
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
    MusikidalarnaSeCrawler().run()


if __name__ == '__main__':
    main()
