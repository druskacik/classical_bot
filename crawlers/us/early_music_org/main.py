import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.early-music.org/'
SOURCE = 'Texas Early Music Project'
CITY = 'Austin'

SEASON_PATHS = [
    '2010-2011-season',
    '2011-2012-season',
    '2012-2013-season',
    '2013-2014-season',
    '2014-2015-season',
    '2015-2016-season',
    '2016-2017-season',
    '2017-2018-season',
    '2018-2019-season',
    '2019-2020-season',
    '2020-2021-season',
    'temp-2021-2022-season',
    'temp-2022-2023-season',
    '2023-2024-season',
    '20242025-artistic-season',
    '2025-2026-artistic-season',
    '20262027-season',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|'
    r'Sep(?:t)?\.?|Oct\.?|Nov\.?|Dec\.?' 
)
DATE_RE = re.compile(
    rf'(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    rf'(?:\s*(?:&|and|–|-)\s*(?:(?P<month2>{MONTH})\s+)?'
    rf'(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?)?\s*,?\s*(?P<year>20\d{{2}})',
    re.IGNORECASE,
)
MONTH_DAY_RE = re.compile(
    rf'(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    rf'(?:\s*(?:&|and|–|-)\s*(?:(?P<month2>{MONTH})\s+)?'
    rf'(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?)?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(?:at\s+)?(\d{1,2}(?::\d{2})?)\s*([ap])\.?m\.?', re.I)
VENUE_RE = re.compile(
    r'\b(?:church|cathedral|chapel|parish|parrish|presbyterian|lutheran|'
    r'episcopal|synagogue|temple|sanctuary|concert hall|recital hall|theatre|'
    r'theater|arts center|performing arts center|university|museum)\b',
    re.IGNORECASE,
)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip(' \t\r\n,;|')


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    raw = f'{match.group(1)} {match.group(2).upper()}M'
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(raw, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def expand_dates(value):
    match = DATE_RE.search(value or '')
    if not match:
        return []
    month = match.group('month').rstrip('.')[:3]
    month2 = (match.group('month2') or match.group('month')).rstrip('.')[:3]
    year = match.group('year')
    results = []
    for item_month, day in ((month, match.group('day')), (month2, match.group('day2'))):
        if not day:
            continue
        try:
            results.append(datetime.strptime(f'{item_month} {day} {year}', '%b %d %Y').date().isoformat())
        except ValueError:
            continue
    return results


def heading_title(heading):
    text = clean_text(heading.get_text(' ', strip=True))
    match = DATE_RE.search(text)
    if match:
        after = clean_text(text[match.end():].lstrip(' :–-'))
        before = clean_text(text[:match.start()].rstrip(' :–-'))
        return after or before
    return text


def element_tokens(element):
    return [clean_text(item) for item in element.stripped_strings if clean_text(item)]


def add_inferred_years(tokens, page_url):
    season = re.search(r'(20\d{2})[-/](?:20)?(\d{2})', page_url)
    if not season:
        return tokens
    start_year = int(season.group(1))
    end_year = int(f'{str(start_year)[:2]}{season.group(2)}')
    enriched = []
    for token in tokens:
        match = MONTH_DAY_RE.search(token)
        if match and not DATE_RE.search(token):
            month_number = datetime.strptime(match.group('month').rstrip('.')[:3], '%b').month
            year = start_year if month_number >= 7 else end_year
            token = f'{token[:match.end()]} {year}{token[match.end():]}'
        enriched.append(token)
    return enriched


def venue_from_tokens(tokens):
    candidates = []
    for token in tokens:
        value = clean_text(re.sub(r'^at\s+', '', token, flags=re.I))
        value = re.split(r'\s*,\s*\d{2,5}\b', value, maxsplit=1)[0]
        if VENUE_RE.search(value) and len(value) <= 120:
            embedded = re.search(
                r'\bat\s+([A-Z“”\'’][A-Za-z“”\'’.-]*(?:\s+[A-Z“”\'’][A-Za-z“”\'’.-]*){0,6}\s+'
                r'(?:Church|Cathedral|Chapel|Parish|Presbyterian|Lutheran|University|Temple))\b',
                value,
            )
            if embedded:
                value = embedded.group(1)
            candidates.append(value)
    return candidates[0] if candidates else None


def description_from_tokens(tokens):
    parts = []
    for token in tokens:
        if DATE_RE.search(token) or TIME_RE.search(token) or VENUE_RE.search(token):
            continue
        if re.search(r'\b(ticket|purchase|donate|pricing|admission)\b', token, re.I):
            continue
        if len(token) >= 80 and token not in parts:
            parts.append(token)
    return '\n\n'.join(parts) or None


def parse_block(block, page_url):
    headings = block.find_all(['h1', 'h2', 'h3', 'h4'])
    dated_headings = [heading for heading in headings if DATE_RE.search(heading.get_text(' ', strip=True))]
    groups = []

    if dated_headings:
        for heading in dated_headings:
            tokens = element_tokens(heading)
            sibling = heading.find_next_sibling()
            while sibling and not (isinstance(sibling, Tag) and sibling.name in {'h1', 'h2', 'h3', 'h4'}):
                tokens.extend(element_tokens(sibling))
                sibling = sibling.find_next_sibling()
            groups.append((heading_title(heading), add_inferred_years(tokens, page_url)))
    else:
        tokens = add_inferred_years(element_tokens(block), page_url)
        title = heading_title(headings[0]) if headings else ''
        if not title:
            first_date = next((index for index, token in enumerate(tokens) if DATE_RE.search(token)), None)
            if first_date:
                title_parts = list(tokens[:first_date])
                while title_parts and re.fullmatch(
                    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?',
                    title_parts[-1], re.I,
                ):
                    title_parts.pop()
                title = clean_text(' '.join(title_parts))
        if title and any(DATE_RE.search(token) for token in tokens):
            groups.append((title, tokens))

    records = []
    for title, tokens in groups:
        if not title or re.search(r'\b(season tickets|download|assistance)\b', title, re.I):
            continue
        dated = [(index, token, expand_dates(token)) for index, token in enumerate(tokens) if expand_dates(token)]
        if not dated:
            continue
        group_venue = venue_from_tokens(tokens)
        description = description_from_tokens(tokens)
        for position, (index, token, dates) in enumerate(dated):
            end = dated[position + 1][0] if position + 1 < len(dated) else len(tokens)
            local_venue = venue_from_tokens(tokens[index:end]) or group_venue
            local_time = parse_time(' '.join(tokens[index:end]))
            for event_date in dates:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': page_url,
                    'time_from': local_time,
                    'venue': local_venue,
                    'city': 'Lakeway' if local_venue and 'Lakeway' in ' '.join(tokens[index:end]) else CITY,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })
    return records


def apply_page_defaults(records, page_text):
    # The 2025-26 page publishes venue and time rules after its four event entries.
    for record in records:
        if 'All Saturday concerts will begin at 7:30pm' in page_text:
            record['venue'] = (
                "St. Martin's Lutheran Church"
                if 'Cry of many voices' in record['title']
                else 'Redeemer Presbyterian Church'
            )
            weekday = datetime.strptime(record['date'], '%Y-%m-%d').strftime('%A')
            record['time_from'] = '19:30' if weekday == 'Saturday' else '15:00'
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for path in SEASON_PATHS:
        url = f'{SOURCE_URL}{path}'
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Season page request failed', event='crawler_page_failed', level='warning',
                url=url, error_type=type(error).__name__, error_message=str(error),
            )
            continue
        soup = BeautifulSoup(response.text, 'html.parser')
        page = soup.select_one('.sqs-layout[id^="page-"]')
        if not page:
            continue
        page_records = []
        for block in page.select('.sqs-block-content'):
            page_records.extend(parse_block(block, url))
        records.extend(apply_page_defaults(page_records, page.get_text(' ', strip=True)))

    valid = [record for record in records if record['venue']]
    skipped = len(records) - len(valid)
    if skipped:
        log_message(
            'Skipped concerts without a defensible venue', event='crawler_records_skipped',
            level='warning', record_count=skipped,
        )
    return sorted(valid, key=lambda item: (item['date'], item['title'], item['venue']))


class EarlyMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='early_music_org',
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
        return scrape_concerts()


def main():
    EarlyMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
