import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ccmusica.org.uy/'
SOURCE = 'Centro Cultural de Música'
SEASONS_URL = f'{SOURCE_URL}temporadas/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/conciertos'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-UY,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1,
    'febrero': 2,
    'marzo': 3,
    'abril': 4,
    'mayo': 5,
    'junio': 6,
    'julio': 7,
    'agosto': 8,
    'setiembre': 9,
    'septiembre': 9,
    'octubre': 10,
    'noviembre': 11,
    'diciembre': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value, year):
    match = re.search(r'\b(\d{1,2})\s+de\s+([a-záéíóúñ]+)\b', value.lower())
    if not match:
        return None
    month = MONTHS.get(match.group(2))
    if not month:
        return None
    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(
        r'\b([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s*(?:h|hs|hrs)\.?\b',
        value[:500],
        re.IGNORECASE,
    )
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_season_cards(html):
    soup = BeautifulSoup(html, 'html.parser')
    cards = {}
    for card in soup.select('.service-card'):
        title_link = card.select_one('a[href*="/conciertos/"]')
        subtitle = clean_text(card.select_one('.service-card__subtitle'))
        year_heading = card.find_previous('h2', string=re.compile(r'^\s*20\d{2}\s*$'))
        if not title_link or not subtitle or year_heading is None:
            continue

        parts = [part.strip() for part in subtitle.split('|', 1)]
        if len(parts) != 2 or not parts[1]:
            continue
        year = int(clean_text(year_heading))
        event_date = parse_date(parts[0], year)
        if not event_date:
            continue
        url = title_link['href'].split('#', 1)[0].rstrip('/') + '/'
        cards[url] = {'date': event_date, 'venue': parts[1]}
    return cards


class CcmusicaOrgUyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ccmusica_org_uy',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='UY',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            seasons_response = session.get(SEASONS_URL, timeout=45)
            seasons_response.raise_for_status()
            season_cards = parse_season_cards(seasons_response.text)

            posts = []
            page = 1
            while True:
                response = session.get(
                    API_URL,
                    params={
                        'page': page,
                        'per_page': 100,
                        'status': 'publish',
                        '_fields': 'link,title,content',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                posts.extend(response.json())
                total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
                if page >= total_pages:
                    break
                page += 1
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Centro Cultural de Música concerts',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            url = post.get('link', '').split('#', 1)[0].rstrip('/') + '/'
            card = season_cards.get(url)
            title = clean_text(BeautifulSoup(post.get('title', {}).get('rendered', ''), 'html.parser'))
            content = BeautifulSoup(post.get('content', {}).get('rendered', ''), 'html.parser')
            description = clean_text(content)
            if not card or not title or not description:
                continue
            records.append({
                'title': title,
                'date': card['date'],
                'url': url,
                'time_from': parse_time(description),
                'venue': card['venue'],
                'city': 'Montevideo',
                'description': description,
            })

        skipped_count = len(posts) - len(records)
        if skipped_count:
            log_message(
                'Skipped concerts missing required archive or detail data',
                event='crawler_records_skipped',
                level='warning',
                record_count=skipped_count,
            )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CcmusicaOrgUyCrawler().run()


if __name__ == '__main__':
    main()
