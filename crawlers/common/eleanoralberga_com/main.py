import json
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.eleanoralberga.com/'
NEWS_URL = urljoin(SOURCE_URL, 'about')
SOURCE = 'Eleanor Alberga'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

CITY_COUNTRIES = {
    'Aldeburgh': 'GB',
    'Amsterdam': 'NL',
    'Dublin': 'IE',
    'Liverpool': 'GB',
    'London': 'GB',
}

EVENT_TERMS = re.compile(
    r'\b(?:concert|performance|performed|premiere|recital|festival)\b', re.I
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.fullmatch(r'\s*(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\s*', value)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def event_from_json_ld(soup, expected_date, url):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in iter_json_objects(data):
            event_type = item.get('@type')
            if event_type != 'Event' and not (
                isinstance(event_type, list) and 'Event' in event_type
            ):
                continue
            start = clean_text(item.get('startDate'))
            if not start.startswith(expected_date):
                continue
            location = item.get('location') or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            address = location.get('address') or {} if isinstance(location, dict) else {}
            if isinstance(address, str):
                address = {'addressLocality': address}
            venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
            city = clean_text(address.get('addressLocality'))
            country = clean_text(address.get('addressCountry'))
            country_code = country.upper() if re.fullmatch(r'[A-Za-z]{2}', country) else None
            if not country_code:
                country_code = CITY_COUNTRIES.get(city)
            title = clean_text(item.get('name'))
            if not title or not venue or not city or not country_code:
                continue
            time_match = re.search(r'T(\d{2}:\d{2})', start)
            return {
                'title': title,
                'date': expected_date,
                'url': url,
                'time_from': time_match.group(1) if time_match else None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
            }
    return None


def event_from_visible_details(soup, expected_date, url):
    expected = date.fromisoformat(expected_date)
    date_label = f'{expected.day} {expected.strftime("%B %Y")}'
    for text_node in soup.find_all(string=lambda text: text and date_label in text):
        container = text_node.parent.parent if text_node.parent else None
        text = clean_text(container)
        match = re.search(
            rf'{re.escape(date_label)}\s*,?\s*(\d{{1,2}}:\d{{2}})\s*[|\n]\s*'
            r'([^|\n]+),\s*([^|\n]+)',
            text,
        )
        if not match:
            continue
        venue, city = match.group(2).strip(), match.group(3).strip()
        country_code = CITY_COUNTRIES.get(city)
        title = clean_text(soup.select_one('main h1, article h1, h1'))
        if title and venue and city and country_code:
            return {
                'title': title,
                'date': expected_date,
                'url': url,
                'time_from': match.group(1),
                'venue': venue,
                'city': city,
                'country_code': country_code,
            }
    return None


def parse_detail(html, expected_date, url):
    soup = BeautifulSoup(html, 'html.parser')
    record = event_from_json_ld(soup, expected_date, url)
    return record or event_from_visible_details(soup, expected_date, url)


def news_candidates(html):
    soup = BeautifulSoup(html, 'html.parser')
    candidates = []
    for paragraph in soup.select('.sqs-html-content p'):
        event_date = parse_date(clean_text(paragraph))
        if not event_date:
            continue
        description_node = paragraph.find_next_sibling('p')
        description = clean_text(description_node)
        if not description or not EVENT_TERMS.search(description):
            continue
        links = [
            urljoin(NEWS_URL, link['href'])
            for link in description_node.select('a[href]')
            if not link['href'].startswith(('mailto:', '#'))
        ]
        if links:
            candidates.append((event_date, description, links))
    return candidates


class EleanorAlbergaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eleanoralberga_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        response = session.get(NEWS_URL, timeout=45)
        response.raise_for_status()

        records = []
        for event_date, news_description, links in news_candidates(response.text):
            for url in links:
                try:
                    detail_response = session.get(url, timeout=45)
                    detail_response.raise_for_status()
                    record = parse_detail(detail_response.text, event_date, url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch linked Eleanor Alberga event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if not record:
                    continue
                detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
                detail_text = clean_text(detail_soup.select_one('main, article'))
                record.update({
                    'description': '\n\n'.join(
                        part for part in (news_description, detail_text) if part
                    ) or None,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
                records.append(record)

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    EleanorAlbergaComCrawler().run()


if __name__ == '__main__':
    main()
