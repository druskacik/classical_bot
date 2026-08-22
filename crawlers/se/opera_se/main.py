import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera.se/'
SOURCE = 'GöteborgsOperan'
CALENDAR_URL = urljoin(SOURCE_URL, 'forestallningar/kalender/')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'sv-SE,sv;q=0.9,en;q=0.7',
}
MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'maj': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12,
}
EVENT_PATTERN = re.compile(
    r'(\{\\"__typename\\":\\"event\\".*?'
    r'\\"ticketInformation\\":(?:\\"[^\"]*\\"|\\"\\\$undefined\\")\})'
)
DATE_PATTERN = re.compile(
    r'(\d{1,2})\s+(jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)[a-z.]*'
    r'(?:\s+(\d{4}))?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    return urlunsplit(('https', 'www.opera.se', parts.path.rstrip('/') + '/', '', ''))


def normalized(value):
    text = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return re.sub(r'[^a-z0-9]+', '', ''.join(c for c in text if not unicodedata.combining(c)))


def get_response(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response


def calendar_events(content):
    text = content.decode('utf-8', errors='replace')
    events = {}
    for raw in EVENT_PATTERN.findall(text):
        try:
            event = json.loads(json.loads(f'"{raw}"'))
        except (json.JSONDecodeError, TypeError):
            continue
        event_id = str(event.get('id') or '')
        if event_id and isinstance(event.get('date'), dict):
            events[event_id] = event
    return list(events.values())


def sitemap_urls(content):
    soup = BeautifulSoup(content, 'xml')
    urls = []
    for node in soup.select('loc'):
        url = clean_text(node)
        path = urlsplit(url).path
        if re.fullmatch(r'/forestallningar/sasong-\d{4}-\d{4}/[^/]+/', path):
            urls.append(canonical_url(url))
    return sorted(set(urls))


def label_value(soup, label):
    for node in soup.find_all(string=lambda value: clean_text(value).casefold() == label.casefold()):
        parent = node.parent
        if not parent:
            continue
        container = parent.parent
        if container:
            values = [clean_text(value) for value in container.stripped_strings]
            values = [value for value in values if value.casefold() != label.casefold()]
            if values:
                return values[0]
    return ''


def displayed_dates(value, season):
    matches = list(DATE_PATTERN.finditer(clean_text(value).casefold()))
    if not matches:
        return []
    season_start, season_end = (int(part) for part in season.split('-'))
    parsed = []
    for index, match in enumerate(matches):
        day = int(match.group(1))
        month = MONTHS[match.group(2)[:3]]
        if match.group(3):
            year = int(match.group(3))
        elif index + 1 < len(matches) and matches[index + 1].group(3):
            year = int(matches[index + 1].group(3))
        else:
            year = season_start if month >= 7 else season_end
        try:
            parsed.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return list(dict.fromkeys(parsed))


def page_description(soup, title):
    main = soup.find('main')
    if not main:
        return None
    text = clean_text(main)
    title_position = text.casefold().find(clean_text(title).casefold())
    if title_position >= 0:
        text = text[title_position:]
    for marker in ('\nKalender\n', '\nDu kanske också skulle gilla:', '\nVad letar du efter?'):
        if marker in text:
            text = text.split(marker, 1)[0]
    return text or None


def parse_detail(session, url):
    response = get_response(session, url)
    soup = BeautifulSoup(response.content, 'html.parser')
    heading = soup.select_one('main h1')
    title = clean_text(heading)
    season_match = re.search(r'/sasong-(\d{4}-\d{4})/', url)
    played = label_value(soup, 'Spelas')
    venue = label_value(soup, 'Scen')
    if not title or not season_match:
        return None
    return {
        'url': url,
        'title': title,
        'dates': displayed_dates(played, season_match.group(1)),
        'venue': venue,
        'description': page_description(soup, title),
        'season': season_match.group(1),
    }


def city_and_venue(event):
    location = clean_text(event.get('location'))
    scene = clean_text(event.get('scene'))
    if not scene or not location:
        return None, None
    if location == 'GöteborgsOperan':
        return 'Göteborg', scene
    if location == 'Skövde Kulturhus':
        return 'Skövde', scene
    # Touring entries use the municipality as location and a distinct theatre as scene.
    return location, scene


def detail_index(details):
    result = {}
    for detail in sorted(details, key=lambda item: item['season'], reverse=True):
        for key in (normalized(detail['title']),):
            result.setdefault(key, detail)
    return result


class OperaSeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_se',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='SE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        calendar_response = get_response(session, CALENDAR_URL)
        events = calendar_events(calendar_response.content)

        sitemap_response = get_response(session, SITEMAP_URL)
        detail_urls = sitemap_urls(sitemap_response.content)
        details = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(parse_detail, session, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail = future.result()
                    if detail:
                        details.append(detail)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape GöteborgsOperan production detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        index = detail_index(details)
        records = []
        seen = set()
        for event in events:
            event_date = clean_text(event.get('date', {}).get('formatted'))
            try:
                datetime.strptime(event_date, '%Y-%m-%d')
            except ValueError:
                continue
            city, venue = city_and_venue(event)
            title = clean_text(event.get('title'))
            if not all((title, city, venue)):
                continue
            detail = index.get(normalized(event.get('eventGroupTitle'))) or index.get(normalized(title))
            detail_url = clean_text(event.get('url'))
            if detail_url:
                event_url = canonical_url(detail_url)
            elif detail:
                event_url = detail['url']
            else:
                event_url = clean_text((event.get('cta') or {}).get('url')) or CALENDAR_URL
            record = {
                'title': title,
                'date': event_date,
                'url': event_url,
                'time_from': clean_text(event.get('date', {}).get('time')) or None,
                'venue': venue,
                'city': city,
                'country_code': 'SE',
                'description': detail['description'] if detail else None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
            key = (record['title'], record['date'], record['time_from'], venue, city)
            seen.add(key)
            records.append(record)

        today = date.today().isoformat()
        for detail in details:
            venue = detail['venue']
            if not venue or 'turné' in venue.casefold():
                continue
            for event_date in detail['dates']:
                if event_date >= today:
                    continue
                key = (detail['title'], event_date, None, venue, 'Göteborg')
                if key in seen:
                    continue
                seen.add(key)
                records.append({
                    'title': detail['title'],
                    'date': event_date,
                    'url': detail['url'],
                    'time_from': None,
                    'venue': venue,
                    'city': 'Göteborg',
                    'country_code': 'SE',
                    'description': detail['description'],
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    OperaSeCrawler().run()


if __name__ == '__main__':
    main()
