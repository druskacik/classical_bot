import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gyso.cn/'
LISTING_URL = urljoin(SOURCE_URL, 'Pages/ProductList.aspx')
DETAIL_URL = urljoin(SOURCE_URL, 'Pages/ProductDeta.aspx?Pid={pid}')
SOURCE = '贵阳交响乐团'
CITY = '贵阳'
VENUE = '贵阳大剧院音乐厅'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.6',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u3000', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return response


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def parse_description(soup):
    body = soup.select_one('.content_text')
    if body is None:
        return None
    lines = clean_text(body).splitlines()
    # Prices and purchase instructions are not programme information. Keep
    # performers, composers, works, and the remaining editorial description.
    lines = [
        line for line in lines
        if not re.match(r'^(?:票\s*价|购票|订票|售票)\s*[：:]', line)
    ]
    return '\n'.join(lines).strip() or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('.content_w01')
    title_node = heading.select_one('h3') if heading else None
    title = clean_text(title_node)
    heading_text = clean_text(heading)
    match = re.search(
        r'\b(20\d{2}-\d{2}-\d{2})\b.*?'
        r'((?:[01]?\d|2[0-3]):[0-5]\d)(?::[0-5]\d)?',
        heading_text,
        re.S,
    )
    if not title or not match:
        return None
    event_date = valid_date(match.group(1))
    if not event_date:
        return None

    description = parse_description(soup)
    # The public performance calendar belongs to the Guiyang Symphony at its
    # home in Guiyang Grand Theatre. Do not apply that default to a detail that
    # explicitly labels a different performance city or venue.
    labelled_location = re.search(
        r'(?:演出地点|演出场馆|地点|场馆)\s*[：:]\s*([^\n，。；]+)',
        description or '',
    )
    if labelled_location and not re.search(
        r'贵阳(?:大剧院)?|贵阳交响乐团音乐厅', labelled_location.group(1)
    ):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': match.group(2),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'CN',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def archive_upper_bound():
    response = get_response(LISTING_URL)
    ids = [
        int(value) for value in re.findall(
            r'ProductDeta\.aspx\?Pid=(\d+)', response.text, flags=re.I
        )
    ]
    if not ids:
        raise ValueError('Could not discover any concert detail IDs')
    return max(ids)


def fetch_detail(pid):
    url = DETAIL_URL.format(pid=pid)
    response = get_response(url)
    return parse_detail(response.text, url)


def get_concerts():
    upper_bound = archive_upper_bound()
    records = []
    failures = 0
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(fetch_detail, pid): pid
            for pid in range(1, upper_bound + 1)
        }
        for future in as_completed(futures):
            pid = futures[future]
            try:
                record = future.result()
            except requests.RequestException:
                failures += 1
                continue
            if record:
                records.append(record)

    if failures:
        log_message(
            'Some GYSO archive pages could not be fetched',
            event='crawler_items_failed',
            level='warning',
            url=LISTING_URL,
            record_count=failures,
            error_type='RequestException',
            error_message='One or more numeric concert detail pages failed',
        )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class GysoCnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gyso_cn',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CN',
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
    GysoCnCrawler().run()


if __name__ == '__main__':
    main()
