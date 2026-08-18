import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hernandosymphony.com/'
SOURCE = 'Hernando Symphony Orchestra'
TICKET_URL = f'{SOURCE_URL}ticket-concert-info'
MUSIC_URL = f'{SOURCE_URL}music-for-2026-2027'
VENUE = 'St. Frances Cabrini'
CITY = 'Spring Hill'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def _page_asset_url(html, page_alias):
    match = re.search(r'var SiteFilesMap\s*=\s*(\{.*?\});', html, re.DOTALL)
    if not match:
        raise ValueError('SiteFilesMap was not found')
    files = json.loads(match.group(1))

    metadata_match = re.search(r'var DBSiteMetaData\s*=\s*(\{.*?\});\s*var TemporaryImages', html, re.DOTALL)
    if not metadata_match:
        raise ValueError('Page metadata was not found')
    metadata = json.loads(metadata_match.group(1))
    pages = metadata['pagesStructureInformation']['pagesData']
    page_id = next((key for key, value in pages.items() if value.get('urlAlias') == page_alias), None)
    if not page_id or f'page-{page_id}' not in files:
        raise ValueError(f'Page asset was not found for {page_alias}')
    return files[f'page-{page_id}']


def _formatted_text_blocks(script):
    payload = json.loads(script.split('=', 1)[1].strip().rstrip(';'))
    blocks = []

    def visit(value):
        if isinstance(value, dict):
            properties = value.get('elementProperties', {})
            formatted = properties.get('formattedText')
            if isinstance(formatted, str):
                text = BeautifulSoup(formatted, 'html.parser').get_text('\n', strip=True)
                text = re.sub(r'[ \t\xa0]+', ' ', text.replace('\u200b', ''))
                text = re.sub(r'\n{2,}', '\n', text).strip()
                if text:
                    position = properties.get('sizeAndPosition') or {}
                    blocks.append((position.get('left', 0), position.get('top', 0), text))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return blocks


def _programmes(blocks):
    date_pattern = re.compile(r'([A-Z][A-Z ]+)\n([A-Z]+ \d{1,2})\s*&\s*(\d{1,2}),\s*(20\d{2})')
    headings = []
    for left, top, text in blocks:
        match = date_pattern.search(text)
        if match:
            title = match.group(1).strip()
            first = datetime.strptime(f'{match.group(2)} {match.group(4)}', '%B %d %Y').date()
            second = first.replace(day=int(match.group(3)))
            headings.append((left, top, title, {first.isoformat(), second.isoformat()}))

    repertoire = [(left, text) for left, top, text in blocks if top > 400 and not date_pattern.search(text)]
    programmes = {}
    for left, _top, title, dates in headings:
        candidates = [(abs(block_left - left), text) for block_left, text in repertoire]
        description = min(candidates)[1] if candidates else None
        for event_date in dates:
            programmes[event_date] = (title, description)
    return programmes


class HernandoSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hernandosymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
            ticket_page = session.get(TICKET_URL, timeout=45)
            ticket_page.raise_for_status()
            music_page = session.get(MUSIC_URL, timeout=45)
            music_page.raise_for_status()
            ticket_asset = session.get(_page_asset_url(ticket_page.text, 'ticket-concert-info'), timeout=45)
            ticket_asset.raise_for_status()
            music_asset = session.get(_page_asset_url(music_page.text, 'music-for-2026-2027'), timeout=45)
            music_asset.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Hernando Symphony concert data',
                event='crawler_fetch_failed', level='error', url=SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        ticket_blocks = _formatted_text_blocks(ticket_asset.text)
        programmes = _programmes(_formatted_text_blocks(music_asset.text))
        records = []
        schedule_pattern = re.compile(
            r'(SATURDAY|SUNDAY)\s+(\d{1,2}:\d{2})\s*(AM|PM)?\n(.*)', re.DOTALL
        )
        for _left, _top, text in ticket_blocks:
            match = schedule_pattern.fullmatch(text)
            if not match:
                continue
            hour, minute = map(int, match.group(2).split(':'))
            if match.group(3) == 'PM' and hour < 12:
                hour += 12
            for value in re.findall(r'[A-Z]+ \d{1,2}, 20\d{2}', match.group(4)):
                event_date = datetime.strptime(value, '%B %d, %Y').date().isoformat()
                title, description = programmes.get(event_date, ('Hernando Symphony Orchestra Concert', None))
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': MUSIC_URL,
                    'time_from': f'{hour:02d}:{minute:02d}',
                    'venue': VENUE,
                    'city': CITY,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
        return sorted(records, key=lambda record: (record['date'], record['time_from']))


def main():
    HernandoSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
