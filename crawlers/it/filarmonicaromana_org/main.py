import ast
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filarmonicaromana.org/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'Programmazione')
SOURCE = 'Accademia Filarmonica Romana'
GRID_NAME = 'ctl00$ContentPlaceHolder1$ASPxGridView1'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def initial_grid_state(response):
    soup = BeautifulSoup(response.content, 'html.parser')
    fields = {
        node['name']: node.get('value', '')
        for node in soup.select('input[type="hidden"][name]')
    }
    match = re.search(
        r"'stateObject':\{'keys':(\[[^]]+\]),'callbackState':'([^']+)'",
        response.text,
    )
    if not match:
        raise ValueError('Could not find programme grid callback state')
    keys = ast.literal_eval(match.group(1))
    state = {'keys': keys, 'callbackState': match.group(2)}
    fields[GRID_NAME] = html.escape(json.dumps(state, separators=(',', ':')), quote=True)
    return soup, fields, keys


def callback_page(session, fields, keys, page_index):
    keys_json = json.dumps(keys, separators=(',', ':'))
    data = dict(fields)
    data['__CALLBACKID'] = GRID_NAME
    data['__CALLBACKPARAM'] = (
        f'c0:KV|{len(keys_json)};{keys_json};GB|20;12|'
        f'PAGERONCLICK3|PN{page_index};'
    )
    response = session.post(
        PROGRAMME_URL,
        data=data,
        headers={'X-Requested-With': 'XMLHttpRequest'},
        timeout=45,
    )
    response.raise_for_status()
    html_match = re.search(r"'html':'(.*)'\}\}\)\s*$", response.text, re.S)
    if not html_match:
        raise ValueError(f'Could not parse programme grid page {page_index + 1}')
    fragment = html_match.group(1).replace(r'<\/', '</')
    fragment = fragment.replace(r'\r', '\r').replace(r'\n', '\n').replace(r'\t', '\t')
    return BeautifulSoup(fragment, 'html.parser')


def grid_occurrences(soup):
    rows = []
    for row in soup.select('tr[id*="_DXDataRow"]'):
        link = row.select_one('a[href*="Concerto/"]')
        detail_cells = row.select('table.templateTable tr:nth-of-type(2) > td')
        title_node = row.select_one('[id*="_lblTitolo_"]')
        if len(detail_cells) < 3 or link is None or title_node is None:
            continue
        values = [clean_text(cell) for cell in detail_cells]
        try:
            event_date = datetime.strptime(values[0], '%d/%m/%Y').date().isoformat()
        except ValueError:
            continue
        venue = values[2]
        title = clean_text(title_node)
        if not title or not venue:
            continue
        time_match = re.fullmatch(r'(\d{1,2}):(\d{2})', values[1])
        time_from = None
        if time_match and 0 <= int(time_match.group(1)) <= 23:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        rows.append({
            'title': title,
            'date': event_date,
            'url': urljoin(PROGRAMME_URL, link.get('href')),
            'time_from': time_from,
            'venue': venue,
            'city': 'Roma',
        })
    return rows


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    if '/Concerto/' not in response.url:
        return None
    soup = BeautifulSoup(response.content, 'html.parser')
    content = soup.select_one('.fr-view')
    if content is None:
        return None
    for node in content.select('a.linkBiglietti, img, h2, h6'):
        node.decompose()
    return clean_text(content) or None


class FilarmonicaRomanaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filarmonicaromana_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(PROGRAMME_URL, timeout=45)
        response.raise_for_status()
        first_soup, fields, keys = initial_grid_state(response)

        page_match = re.search(r'Pagina\s+\d+\s+di\s+(\d+)', clean_text(first_soup))
        page_count = int(page_match.group(1)) if page_match else 1
        records = grid_occurrences(first_soup)
        for page_index in range(1, page_count):
            records.extend(grid_occurrences(callback_page(session, fields, keys, page_index)))

        descriptions = {}
        urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_description, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except (requests.RequestException, ValueError) as error:
                    descriptions[url] = None
                    log_message(
                        'Failed to fetch Filarmonica Romana event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    FilarmonicaRomanaOrgCrawler().run()


if __name__ == '__main__':
    main()
