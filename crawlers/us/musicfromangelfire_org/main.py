import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musicfromangelfire.org/'
SOURCE = 'Music from Angel Fire'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}
DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tues|Wed|Thurs|Fri|Sat|Sun)?[,]?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December|Aug)'
    r'\s+(\d{1,2}),\s*(20\d{2})\b',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?', re.I)


def clean_text(element):
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    month = 'August' if match.group(1).lower() == 'aug' else match.group(1)
    try:
        return datetime.strptime(
            f'{month} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_location(text):
    lines = [line.strip(' ,|') for line in text.splitlines() if line.strip(' ,|')]
    candidates = []
    for line in lines:
        before_date = DATE_RE.split(line, maxsplit=1)[0].strip(' ,|') if DATE_RE.search(line) else line
        if before_date:
            candidates.append(before_date)

    location_rules = (
        (r'Taos (?:Community Auditorium|Center for the Arts)', 'Taos'),
        (r'(?:Shuler Theater|Schuler Theater|Raton Shuler Theatre)', 'Raton'),
        (r'(?:Las Vegas )?Ilfeld Auditorium', 'Las Vegas'),
        (r'Eagle Nest Community Center', 'Eagle Nest'),
        (r'(?:United Church of Angel Fire|Angel Fire Community Center|'
         r'(?:Baptist Church|Angel Fire Baptist Church|Baptist Church of Angel Fire)|'
         r'Moreno Valley Preparatory)', 'Angel Fire'),
    )
    for line in candidates[:6]:
        for pattern, city in location_rules:
            match = re.search(pattern, line, re.I)
            if match:
                return match.group(0).strip(), city
    return None


def eligible_title(title):
    return not re.search(r'\b(?:MUSIC 10[12]|MEET THE COMPOSER)', title, re.I)


def make_record(title, body, url):
    title = clean_text(title).strip(' -–')
    body = clean_text(body)
    event_date = parse_date(body)
    location = parse_location(body)
    if not title or not eligible_title(title) or not event_date or not location:
        return None
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(body),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': body or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_modern_page(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    records = []
    blocks = soup.select('.eb-tab-wrapper')
    if not blocks:
        blocks = [
            widget for widget in soup.select('.so-widget-sow-editor')
            if widget.find(['h3', 'h4']) and DATE_RE.search(clean_text(widget))
        ]
    for index, block in enumerate(blocks, 1):
        heading = block.find(['h3', 'h4'])
        if heading is None:
            continue
        record = make_record(
            heading,
            block,
            f"{page['link']}#event-{index}",
        )
        if record:
            records.append(record)
    return records


def parse_legacy_page(page):
    text = clean_text(BeautifulSoup(page['content']['rendered'], 'html.parser'))
    matches = list(DATE_RE.finditer(text))
    records = []
    for index, match in enumerate(matches, 1):
        end = matches[index].start() if index < len(matches) else len(text)
        segment = text[match.start():end].strip()
        lines = segment.splitlines()
        if len(lines) < 3:
            continue
        title = lines[2]
        if len(title) == 1 and len(lines) > 3:
            title += lines[3]
        record = make_record(title, segment, f"{page['link']}#event-{index}")
        if record:
            records.append(record)
    return records


class MusicFromAngelFireOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicfromangelfire_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(
                API_URL,
                params={
                    'search': 'concerts',
                    'per_page': 100,
                    '_fields': 'id,slug,link,content',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Music from Angel Fire concert archive',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        pages = [page for page in pages if re.fullmatch(r'20\d{2}-concerts', page['slug'])]
        for page in pages:
            parsed = parse_modern_page(page)
            if not parsed:
                parsed = parse_legacy_page(page)
            records.extend(parsed)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MusicFromAngelFireOrgCrawler().run()


if __name__ == '__main__':
    main()
