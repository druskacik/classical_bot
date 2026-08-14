import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera.szczecin.pl/'
SOURCE = 'Opera na Zamku w Szczecinie'
HOME_VENUE = 'Opera na Zamku w Szczecinie'
HOME_CITY = 'Szczecin'
FIRST_YEAR = 2019

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

# Inflected forms occur in Polish venue names and addresses. These are kept
# deliberately explicit: an unknown touring location is skipped rather than
# silently assigned to the opera's home city.
CITY_FORMS = {
    'szczecin': 'Szczecin',
    'szczecinie': 'Szczecin',
    'świnoujście': 'Świnoujście',
    'świnoujściu': 'Świnoujście',
    'dobra': 'Dobra',
    'dobrej': 'Dobra',
    'stargard': 'Stargard',
    'stargardzie': 'Stargard',
    'koszalin': 'Koszalin',
    'koszalinie': 'Koszalin',
    'kołobrzeg': 'Kołobrzeg',
    'kołobrzegu': 'Kołobrzeg',
    'gryfino': 'Gryfino',
    'gryfinie': 'Gryfino',
    'goleniów': 'Goleniów',
    'goleniowie': 'Goleniów',
    'kamień pomorski': 'Kamień Pomorski',
    'kamieniu pomorskim': 'Kamień Pomorski',
    'międzyzdroje': 'Międzyzdroje',
    'międzyzdrojach': 'Międzyzdroje',
    'berlin': 'Berlin',
    'berlinie': 'Berlin',
}

SZCZECIN_VENUE_MARKERS = (
    'jasne błonia',
    'zamek książąt pomorskich',
    'dziedziniec zamku',
    'tarasy zachodnie',
    'teatr letni',
    'filharmonia im. mieczysława karłowicza',
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def valid_date(day, month, year):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def calendar_urls():
    # The repertoire archive begins in 2019. Include the next calendar year so
    # productions announced far ahead are collected without code changes.
    final_year = date.today().year + 1
    return [
        urljoin(SOURCE_URL, f'repertuar/{year}{month:02d}')
        for year in range(FIRST_YEAR, final_year + 1)
        for month in range(1, 13)
    ]


def parse_calendar(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    occurrences = []
    for item in soup.select('main article.event.event--teaser'):
        day_node = item.select_one('.event__teaser-small__day')
        month_node = item.select_one('.event__teaser-small__date')
        time_node = item.select_one('.event__teaser-small__time')
        link = item.select_one('.event__teaser-small__performance > a[href]')
        title_node = item.select_one('.performance__teaser-small__descrition h2')
        if not all((day_node, month_node, link, title_node)):
            continue
        month_match = re.search(r'(\d{1,2})\s*/\s*(\d{4})', clean_text(month_node))
        if not month_match:
            continue
        event_date = valid_date(clean_text(day_node), *month_match.groups())
        title = clean_text(title_node)
        detail_url = urljoin(url, link.get('href', '').strip())
        time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(time_node))
        if not event_date or not title or not detail_url:
            continue
        occurrences.append({
            'title': title,
            'date': event_date,
            'url': detail_url,
            'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        })
    return occurrences


def infer_city(text):
    folded = text.casefold()
    matches = []
    for form, city in CITY_FORMS.items():
        match = re.search(rf'(?<!\w){re.escape(form)}(?!\w)', folded)
        if match:
            matches.append((match.start(), -len(form), city))
    if matches:
        return min(matches)[2]
    if any(marker in folded for marker in SZCZECIN_VENUE_MARKERS):
        return HOME_CITY
    return None


def parse_detail(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    place_node = soup.select_one('.performance__place-text .field__item')
    if place_node:
        place_lines = [clean_text(part) for part in place_node.stripped_strings]
        place_lines = [part for part in place_lines if part]
        venue = place_lines[0] if place_lines else ''
        city = infer_city(' '.join(place_lines))
        if not venue or not city:
            return None
    else:
        venue = HOME_VENUE
        city = HOME_CITY

    description_parts = []
    for selector in (
        '.performance__short-descript',
        '.performance__body',
        '.performance__creators-text',
        '.performance__cast-text',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)
    return {
        'venue': venue,
        'city': city,
        'description': '\n\n'.join(description_parts) or None,
    }


def fetch_calendar(url):
    try:
        return parse_calendar(url, get_response(url).text)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Opera na Zamku calendar month',
            event='crawler_page_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []


def fetch_detail(url):
    try:
        return url, parse_detail(url, get_response(url).text)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Opera na Zamku event detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return url, None


def get_concerts():
    occurrences = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_calendar, url) for url in calendar_urls()]
        for future in as_completed(futures):
            occurrences.extend(future.result())

    detail_urls = sorted({item['url'] for item in occurrences})
    details = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_detail, url) for url in detail_urls]
        for future in as_completed(futures):
            url, detail = future.result()
            details[url] = detail

    records = []
    for occurrence in occurrences:
        detail = details.get(occurrence['url'])
        if not detail:
            continue
        records.append({
            **occurrence,
            **detail,
            'country_code': 'PL',
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class OperaSzczecinPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_szczecin_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaSzczecinPlCrawler().run()


if __name__ == '__main__':
    main()
