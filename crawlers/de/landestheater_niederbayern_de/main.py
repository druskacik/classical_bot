import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.landestheater-niederbayern.de/'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Landestheater Niederbayern'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'märz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def event_urls(session):
    sitemap = get_soup(session, SITEMAP_URL, 'xml')
    urls = []
    for node in sitemap.find_all('loc'):
        url = clean_text(node.get_text())
        if re.match(r'^https://www\.landestheater-niederbayern\.de/event/[^/]+/?$', url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_date(value):
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s+(\d{4})', value
    )
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(value):
    # All performance locations are rendered as "city - venue". Keeping this
    # strict prevents ticket information or a bare city becoming a venue.
    parts = re.split(r'\s+[-–]\s+', clean_text(value), maxsplit=1)
    if len(parts) != 2:
        return None, None
    city, venue = (part.strip() for part in parts)
    if not city or not venue or city.casefold() == venue.casefold():
        return None, None
    return city, venue


def event_description(soup):
    parts = []
    subtitle = clean_text(soup.select_one('.shmtheme_content__post_untertitel'))
    body = clean_text(soup.select_one('.shmtheme_content__post_content'))
    for value in (subtitle, body):
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def parse_event(url, soup):
    title = clean_text(soup.select_one('.shmtheme_content__post_title h1'))
    description = event_description(soup)
    records = []
    if not title:
        return records

    for wrapper in soup.select('.shmtheme_single_event__termine__date_wrapper'):
        header = clean_text(wrapper.select_one('.shmtheme_single_event__termine__header'))
        event_date = parse_date(header)
        if not event_date:
            continue
        for performance in wrapper.select('.shmtheme_single_termin'):
            cancelled = clean_text(performance.get('data-cancelled'))
            if cancelled and cancelled not in ('0', 'false'):
                continue
            location_node = performance.select_one('.shmtheme_single_termin_info.location span')
            city, venue = parse_location(clean_text(location_node))
            time_node = performance.select_one('.shmtheme_single_termin_info.time span')
            time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', clean_text(time_node))
            time_from = None
            if time_match and int(time_match.group(1)) < 24:
                time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if not city or not venue:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'DE',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(url, future.result()))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class LandestheaterNiederbayernDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='landestheater_niederbayern_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LandestheaterNiederbayernDeCrawler().run()


if __name__ == '__main__':
    main()
