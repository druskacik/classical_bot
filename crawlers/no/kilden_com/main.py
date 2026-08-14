import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kilden.com/'
SOURCE = 'Kilden teater og konserthus'
PROGRAM_URL = urljoin(SOURCE_URL, '/program/')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}

# Kilden is a mixed venue. These first-party tags form a deliberately broad
# candidate feed; the potential-event classifier makes the final inclusion
# decision. In particular, the Klassisk filter alone omits some film concerts,
# ballet, musicals, and other eligible crossover performances.
CANDIDATE_TAGS = {
    'Klassisk', 'Konsert', 'KSO', 'Kammerkonserter', 'Opera', 'Musikk',
    'Julekonsert', 'Kor', 'Musikal', 'ballet', 'Dans', 'Filmkonsert',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'des': 12,
}

TOUR_CITIES = {
    'bomuldsfabriken kunsthall': 'Arendal',
    'spira kulturhus': 'Flekkefjord',
    'buen kulturhus': 'Mandal',
    'bykle kirke': 'Bykle',
    'grimstad kulturhus': 'Grimstad',
    'trefoldighetskirken': 'Arendal',
    'vennesla kulturhus': 'Vennesla',
    'frydendal kirke': 'Risør',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_for(title, venue, tags):
    if 'Kilden ut i Agder' not in tags:
        return 'Kristiansand'
    evidence = f'{title} {venue}'.casefold()
    for marker, city in TOUR_CITIES.items():
        if marker in evidence:
            return city
    # These named venues are all in Kristiansand municipality.
    if any(marker in evidence for marker in (
        'knuden', 'kunstsilo', 'søm kirke', 'vågsbygd kirke', 'grim kirke',
        'søgne hovedkirke', 'oddernes kirke', 'kristiansand domkirke',
        'amalienborg',
    )):
        return 'Kristiansand'
    return None


def parse_occurrence(node):
    month_key = (node.get('data-months') or '').strip().casefold()
    month_match = re.fullmatch(r'([a-zæøå]+)-(20\d{2})', month_key)
    date_match = re.search(
        r'(\d{1,2})[.\s]+(' + '|'.join(MONTHS) + r')\b',
        clean_text(node.select_one('.list__item--event__date-and-time')).casefold(),
    )
    if not month_match or not date_match:
        return None
    year = int(month_match.group(2))
    month = MONTHS[date_match.group(2)]
    if month != MONTHS.get(month_match.group(1)[:3]):
        return None
    try:
        event_date = date(year, month, int(date_match.group(1))).isoformat()
    except ValueError:
        return None

    time_match = re.search(
        r'\b(\d{1,2})[.:](\d{2})\b',
        clean_text(node.select_one('.list__item--event__date-and-time')),
    )
    time_from = None
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if hour < 24 and minute < 60:
            time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for node in soup.select('.list__item.list__item--event'):
        tags = {
            clean_text(tag)
            for tag in node.select('.list__item--event__categories__category')
        }
        if not tags.intersection(CANDIDATE_TAGS):
            continue
        link = node.select_one('a.list__item__link[href]')
        title = clean_text(node.select_one('.list__item--event__title'))
        venue = clean_text(node.select_one('.list__item--event__hall span'))
        occurrence = parse_occurrence(node)
        url = urljoin(PROGRAM_URL, link.get('href')) if link else ''
        city = city_for(title, venue, tags)
        if not title or not venue or not url or not occurrence or not city:
            continue
        records.append({
            'title': title,
            'date': occurrence[0],
            'url': url,
            'time_from': occurrence[1],
            'venue': venue,
            'city': city,
        })
    return records


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('main .content')
    return clean_text(content) or None


class KildenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kilden_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(PROGRAM_URL, timeout=45)
        response.raise_for_status()
        records = parse_listing(response.text)

        descriptions = {}
        urls = list(dict.fromkeys(record['url'] for record in records))
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=45): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail = future.result()
                    detail.raise_for_status()
                    descriptions[url] = parse_description(detail.text)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Kilden event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    return KildenComCrawler().run()


if __name__ == '__main__':
    main()
