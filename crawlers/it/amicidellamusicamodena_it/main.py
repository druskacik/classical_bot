import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://amicidellamusicamodena.it/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Amici della Musica di Modena'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_json(session, path, params=None):
    response = session.get(f'{API_URL}/{path}', params=params, timeout=45)
    response.raise_for_status()
    return response


def season_tag_ids(session):
    response = get_json(session, 'tags', {'per_page': 100})
    return [
        item['id'] for item in response.json()
        if re.fullmatch(r'Stagione\s+\d{4}', html.unescape(item.get('name', '')), re.I)
    ]


def event_posts(session, tag_ids):
    posts = []
    page = 1
    while True:
        response = get_json(
            session,
            'posts',
            {
                'tags': ','.join(str(tag_id) for tag_id in tag_ids),
                'tags_relation': 'OR',
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
                '_fields': 'id,link,slug',
            },
        )
        posts.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            return posts
        page += 1


def parse_event(soup, url):
    date_text = clean_text(soup.select_one('#page-header .data, .heading-concerti .data'))
    location_text = clean_text(soup.select_one('#page-header .luogo, .heading-concerti .luogo'))
    title = clean_text(soup.select_one('#page-header h1, .heading-concerti h1'))
    body = soup.select_one('article.type-post')

    date_match = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', date_text)
    time_match = re.search(r'\bore\s+(\d{1,2})[.:](\d{2})\b', date_text, re.I)
    location_parts = re.split(r'\s*[›>]\s*', location_text, maxsplit=1)
    if not date_match or len(location_parts) != 2 or not title:
        return None

    city, venue = (part.strip(' \n,') for part in location_parts)
    if not city or not venue:
        return None
    # The 2021 archive contains a web-only broadcast presented in the same
    # season grid as concerts. It is not an in-person performance occurrence.
    if re.search(r'\b(streaming|diretta sul sito)\b', location_text, re.I):
        return None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None

    time_from = None
    if time_match and 0 <= int(time_match.group(1)) <= 23:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': clean_text(body) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class AmicidellamusicamodenaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amicidellamusicamodena_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
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
            tag_ids = season_tag_ids(session)
            posts = event_posts(session, tag_ids)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Amici della Musica di Modena event index',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for post in posts:
            url = post['link']
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event(BeautifulSoup(response.content, 'html.parser'), url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped Amici della Musica di Modena page without complete event metadata',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Amici della Musica di Modena event',
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
    AmicidellamusicamodenaItCrawler().run()


if __name__ == '__main__':
    main()
