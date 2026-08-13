import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ozawa-festival.com/'
SOURCE = 'Seiji Ozawa Matsumoto Festival'
PROGRAMS_API = f'{SOURCE_URL}wp-json/wp/v2/programs'
PROGRAM_YEARS_API = f'{SOURCE_URL}wp-json/wp/v2/program_year'

# Open-event (7) mixes potentially eligible live concerts with exhibitions,
# parades, overviews, and recorded screen presentations. It must be retained in
# the candidate feed so eligible performances are not lost, then classified.
CANDIDATE_CATEGORY_IDS = (3, 4, 5, 6, 7)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en;q=0.6',
}

VENUE_CITIES = {
    'キッセイ文化ホール': 'Matsumoto',
    '松本文化会館': 'Matsumoto',
    'まつもと市民芸術館': 'Matsumoto',
    '松本市音楽文化ホール': 'Matsumoto',
    'ザ・ハーモニーホール': 'Matsumoto',
    '松本市あがたの森文化会館': 'Matsumoto',
    'ホクト文化ホール': 'Nagano',
    '長野県県民文化会館': 'Nagano',
    '長野県伊那文化会館': 'Ina',
    '松本市立博物館': 'Matsumoto',
    '上土劇場': 'Matsumoto',
    '松本城': 'Matsumoto',
    '信毎メディアガーデン': 'Matsumoto',
    '奥志賀高原ホテル': 'Yamanouchi',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_for_venue(venue):
    for hint, city in VENUE_CITIES.items():
        if hint in venue:
            return city
    return None


def table_value(table, label):
    for row in table.select('tr'):
        heading = row.find('th')
        value = row.find('td')
        if heading and value and clean_text(heading) == label:
            value = copy(value)
            for node in value.select('.venue-options'):
                node.decompose()
            return clean_text(value).replace('\n', ' ')
    return ''


def parse_occurrences(section, default_venue):
    occurrences = []
    for date_list in section.select('.event-date'):
        local_venue_node = date_list.select_one('li.venue')
        venue = clean_text(local_venue_node).replace('\n', ' ') if local_venue_node else default_venue
        city = city_for_venue(venue)
        if not venue or not city:
            continue
        for item in date_list.select(':scope > li:not(.venue)'):
            raw = clean_text(item).replace('\n', ' ')
            match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', raw)
            if not match:
                continue
            try:
                event_date = date(*map(int, match.groups())).isoformat()
            except ValueError:
                continue
            times = re.findall(r'(?<!\d)([0-2]?\d:[0-5]\d)', raw)
            valid_times = [value for value in times if int(value.split(':')[0]) < 24]
            for time_from in valid_times or [None]:
                occurrences.append((event_date, time_from, venue, city))
    return occurrences


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    section = soup.select_one('main section')
    title_node = section.select_one('.program-title h2') if section else None
    table = section.select_one('table.program-table') if section else None
    title = clean_text(title_node).replace('\n', ' ')
    if not all((section, title, table)):
        return []

    default_venue = table_value(table, '会場')
    occurrences = parse_occurrences(section, default_venue)

    description_node = copy(section)
    for row in description_node.select('tr'):
        heading = row.find('th')
        if heading and clean_text(heading) in {'料金', '公演日程', '会場', '公演時間'}:
            row.decompose()
    for node in description_node.select(
        '.venue-options, .prices, .price-caution, .seats, script, style, form'
    ):
        node.decompose()
    description = clean_text(description_node) or None

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'JP',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from, venue, city in occurrences
    ]


def listing_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            PROGRAMS_API,
            params={
                'program_category': ','.join(map(str, CANDIDATE_CATEGORY_IDS)),
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link',
            },
            timeout=60,
        )
        response.raise_for_status()
        urls.extend(item['link'] for item in response.json() if item.get('link'))
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            break
        page += 1
    return list(dict.fromkeys(urls))


def parse_open_events(html, year, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('li[id^="event"]'):
        title = clean_text(item.select_one('h2')).replace('\n', ' ')
        outline = clean_text(item.select_one('.program-outline'))
        if not title:
            continue
        for schedules in item.select('.schedules'):
            venue = clean_text(schedules.select_one('.venue')).replace('\n', ' ')
            city = city_for_venue(venue)
            if not venue or not city:
                continue
            for schedule in schedules.select('.schedule'):
                raw_date = clean_text(schedule.select_one('strong'))
                match = re.search(r'(\d{1,2})月(\d{1,2})日', raw_date)
                if not match:
                    continue
                try:
                    event_date = date(year, *map(int, match.groups())).isoformat()
                except ValueError:
                    continue
                raw_time = clean_text(schedule.select_one('time'))
                time_match = re.search(r'(?<!\d)([0-2]?\d:[0-5]\d)', raw_time)
                time_from = time_match.group(1) if time_match else None
                if time_from and int(time_from.split(':')[0]) >= 24:
                    time_from = None
                elif time_from:
                    hour, minute = time_from.split(':')
                    time_from = f'{int(hour):02d}:{minute}'
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': f'{page_url}#{item.get("id")}',
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'JP',
                    'description': outline or None,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
    return records


def scrape_open_events(session):
    response = session.get(PROGRAM_YEARS_API, params={'per_page': 100}, timeout=60)
    response.raise_for_status()
    records = []
    for term in response.json():
        if not str(term.get('slug', '')).isdigit():
            continue
        year = int(term['slug'])
        page_url = f'{SOURCE_URL}program_category/open-event/?py={year}'
        page = session.get(page_url, timeout=60)
        page.raise_for_status()
        records.extend(parse_open_events(page.text, year, page_url))
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    urls = listing_urls(session)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(requests.get, url, headers=HEADERS, timeout=60): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                records.extend(parse_detail(response.text, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Seiji Ozawa Matsumoto Festival program',
                    event='crawler_detail_fetch_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    records.extend(scrape_open_events(session))
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))


class OzawaFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ozawa_festival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='JP',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OzawaFestivalComCrawler().run()


if __name__ == '__main__':
    main()
