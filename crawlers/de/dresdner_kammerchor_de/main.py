import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dresdner-kammerchor.de/kalender.html'
SOURCE = 'Dresdner Kammerchor'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_location(value):
    parts = [part.strip() for part in value.split(',', 1)]
    if len(parts) != 2 or not all(parts):
        return None
    city, venue = parts
    country_code = 'CZ' if city.casefold() in {'prag', 'praha'} else 'DE'
    return city, venue, country_code


def parse_event(article, detail_soup, url):
    title = clean_text(article.select_one('.event_text_wrapper h2 a[href]'))
    date_element = article.select_one('time[datetime]')
    location = parse_location(clean_text(article.select_one('.location')))
    if not title or date_element is None or not location:
        return None

    raw_date = date_element.get('datetime', '').strip()
    try:
        event_date = date.fromisoformat(raw_date).isoformat()
    except ValueError:
        return None

    time_match = re.search(
        r'\b([01]?\d|2[0-3])[:.]([0-5]\d)(?:\s*Uhr)?\b',
        clean_text(detail_soup.select_one('.infoblock')),
        flags=re.IGNORECASE,
    )
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None

    description_parts = []
    for selector in ('.content-left .rte', '.eventtitel', '.komponist', '.mitwirkende'):
        value = clean_text(detail_soup.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)

    city, venue, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DresdnerKammerchorDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dresdner_kammerchor_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SOURCE_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Dresdner Kammerchor calendar',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for article in soup.select('.mod_eventlist .event'):
            link = article.select_one('.event_text_wrapper h2 a[href]')
            if link is None:
                continue
            url = urljoin(SOURCE_URL, link['href'])
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Dresdner Kammerchor concert detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_event(article, BeautifulSoup(detail_response.text, 'html.parser'), url)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DresdnerKammerchorDeCrawler().run()


if __name__ == '__main__':
    main()
