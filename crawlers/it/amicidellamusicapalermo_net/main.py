import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.amicidellamusicapalermo.net/'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/concerto')
SOURCE = 'Associazione Siciliana Amici della Musica'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+'
        r'(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|'
        r'settembre|ottobre|novembre|dicembre)\s+(\d{4})\b',
        value,
        re.I,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_times(checklist, description):
    times = []
    for value in checklist:
        for hour, minute in re.findall(r'\bore\s+(\d{1,2})[.:](\d{2})\b', value, re.I):
            if 0 <= int(hour) <= 23:
                normalized = f'{int(hour):02d}:{minute}'
                if normalized not in times:
                    times.append(normalized)

    double_show = re.search(
        r'doppia\s+recita.{0,40}?ore\s+(\d{1,2})[.:](\d{2})\s+' 
        r'e\s+(?:ore\s+)?(\d{1,2})[.:](\d{2})',
        description,
        re.I | re.S,
    )
    if double_show:
        for hour, minute in (double_show.group(1, 2), double_show.group(3, 4)):
            if 0 <= int(hour) <= 23:
                normalized = f'{int(hour):02d}:{minute}'
                if normalized not in times:
                    times.append(normalized)
    return times or [None]


def parse_location(checklist):
    for value in checklist:
        parts = [part.strip() for part in value.rsplit(',', 1)]
        if len(parts) != 2:
            continue
        venue, city_text = parts
        city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city_text).strip()
        if city.casefold() == 'politeama garibaldi':
            venue, city = value, 'Palermo'
        if (
            venue
            and city
            and not re.search(r'\b(?:via|viale|piazza|corso)\b', venue, re.I)
            and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]+", city)
        ):
            return venue, city
    return None


def parse_detail(post, soup):
    checklist = [clean_text(item) for item in soup.select('.fusion-checklist li')]
    event_date = next((parsed for value in checklist if (parsed := parse_date(value))), None)
    location = parse_location(checklist)
    title = clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser'))
    url = post.get('link')
    if not event_date or not location or not title or not url:
        return []

    description_soup = BeautifulSoup(post['content']['rendered'], 'html.parser')
    for unwanted in description_soup.select('script, style'):
        unwanted.decompose()
    description = clean_text(description_soup) or None
    venue, city = location
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'IT',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in parse_times(checklist, description or '')
    ]


class AmicidellamusicapalermoNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicidellamusicapalermo_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=45)
            response.raise_for_status()
            posts = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Amici della Musica Palermo concert feed',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            url = post.get('link')
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_detail(post, BeautifulSoup(response.content, 'html.parser')))
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Amici della Musica Palermo event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AmicidellamusicapalermoNetCrawler().run()


if __name__ == '__main__':
    main()
