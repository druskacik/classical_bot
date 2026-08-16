import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chamberorchestra.org/'
SOURCE = 'The Chamber Orchestra of Philadelphia'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    # The site's nginx configuration rejects requests without a crawler or browser UA.
    'User-Agent': 'Googlebot/2.1 (+http://www.google.com/bot.html)',
    'Accept': 'application/json',
}

DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
    r'[,.]?\s+(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2})'
    r'(?:st|nd|rd|th)?[,.]?\s+(?P<year>20\d{2})'
    r'\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)',
    re.IGNORECASE,
)
DATE_PREFIX_RE = re.compile(r'^([A-Z][a-z]+)\s+(\d{1,2})\b')


def clean_text(value):
    if not value:
        return ''
    value = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_date(match):
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}",
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = re.sub(r'\.', '', value).upper().replace(' ', '')
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def section_text(text, start_label, end_labels):
    start = re.search(rf'\b{re.escape(start_label)}\b', text, re.IGNORECASE)
    if not start:
        return ''
    remainder = text[start.end():]
    endings = [
        match.start()
        for label in end_labels
        if (match := re.search(rf'\b{re.escape(label)}\b', remainder, re.IGNORECASE))
    ]
    return remainder[:min(endings)] if endings else remainder


def venue_name(value):
    value = clean_text(value)
    value = re.sub(r'^This performance will take place (?:at|in) (?:the )?', '', value, flags=re.I)
    value = re.sub(r'^[*+~^]+', '', value).strip()
    value = re.sub(r'^([A-Z][a-z]+)\s+\d{1,2}\s+', '', value)
    # Stop before an address. Venue names on this site precede a street number.
    value = re.split(r'\s+\d{1,5}\s+(?=[A-Za-z])', value, maxsplit=1)[0]
    return value.strip(' ,.;')


def city_from_text(value):
    value = clean_text(value)
    for city in ('Philadelphia', 'Villanova', 'Bryn Mawr', 'Holland', 'Newtown'):
        if re.search(rf'\b{re.escape(city)},?\s+PA\s+\d{{5}}\b', value, re.I):
            return city
    return None


def content_blocks(rendered):
    soup = BeautifulSoup(rendered, 'html.parser')
    blocks = []
    for node in soup.select('h1, h2, h3, h4, p'):
        text = clean_text(node)
        if text and text not in blocks:
            blocks.append(text)
    return blocks, clean_text(soup)


def venue_options(blocks, full_text):
    options = []
    in_venue = False
    for block in blocks:
        if re.fullmatch(r'Venue', block, re.I):
            in_venue = True
            continue
        if in_venue and re.fullmatch(r'Duration|Artists?|Program|Information', block, re.I):
            break
        if in_venue and not re.search(r'parking|tickets?|performance[s]? are', block, re.I):
            name = venue_name(block)
            if name and len(name) <= 120:
                options.append({
                    'raw': block,
                    'name': name,
                    'city': city_from_text(block),
                    'prefix': DATE_PREFIX_RE.match(block),
                })

    if options:
        return options

    venue_section = section_text(full_text, 'Venue', ['Duration', 'Information'])
    if venue_section:
        return [{
            'raw': venue_section,
            'name': venue_name(venue_section),
            'city': city_from_text(venue_section),
            'prefix': DATE_PREFIX_RE.match(venue_section),
        }]
    return []


def match_venue(date_match, trailing_text, options, occurrence_index, occurrence_count):
    explicit = re.match(
        r'\s*(?:This performance will take place (?:at|in) (?:the )?)'
        r'(.+?)(?=$|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)',
        trailing_text,
        re.IGNORECASE,
    )
    explicit_name = venue_name(explicit.group(1)) if explicit else ''

    chosen = None
    if explicit_name:
        matching = next(
            (item for item in options if explicit_name.lower() in item['raw'].lower()),
            None,
        )
        raw = matching['raw'] if matching else explicit_name
        offset = raw.lower().find(explicit_name.lower())
        local_text = raw[offset:offset + 250] if offset >= 0 else raw
        chosen = {'name': explicit_name, 'city': city_from_text(local_text), 'raw': local_text}

    if not chosen:
        known = re.search(
            r'(Perelman Theater|Church of the Holy Trinity(?:,? Rittenhouse Square)?|'
            r'Stoneleigh: a natural garden|Bartram[’\']s Garden(?:, Philadelphia)?|'
            r'Esperanza Arts Center|Goodhart Hall(?: at Bryn Mawr College)?|'
            r'Philadelphia Film Society Center|Rhoden Theater(?: at the Pennsylvania Academy of Fine Arts)?|'
            r'Holland Middle School|Newtown Middle School)',
            trailing_text,
            re.I,
        )
        if known:
            chosen = {'name': known.group(1), 'city': city_from_text(trailing_text), 'raw': trailing_text}

    if not chosen:
        month_day = (date_match.group('month').lower(), int(date_match.group('day')))
        for item in options:
            prefix = item.get('prefix')
            if prefix and (prefix.group(1).lower(), int(prefix.group(2))) == month_day:
                chosen = item
                break

    if not chosen and len(options) == 1:
        chosen = options[0]
    if not chosen and len(options) == occurrence_count:
        chosen = options[occurrence_index]

    if not chosen:
        # Older detail templates put the venue directly after each date.
        direct = re.match(r'\s*([^,]{3,80}?)(?=\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|$)', trailing_text)
        if direct:
            name = venue_name(direct.group(1))
            if name and not re.match(r'Artists?|Program|Buy Tickets', name, re.I):
                chosen = {'name': name, 'city': None, 'raw': name}

    if not chosen or not chosen.get('name') or len(re.sub(r'\W', '', chosen['name'])) < 4:
        return None, None
    city = chosen.get('city') or city_from_text(chosen.get('raw', ''))
    if not city:
        matching = next(
            (item for item in options if chosen['name'].lower() in item['raw'].lower()),
            None,
        )
        city = matching.get('city') if matching else None
    if re.search(r'Goodhart Hall|Bryn Mawr College', chosen['name'], re.I):
        city = 'Bryn Mawr'
    elif re.search(r'Stoneleigh', chosen['name'], re.I):
        city = 'Villanova'
    elif re.search(r'Holland Middle School', chosen['name'], re.I):
        city = 'Holland'
    elif re.search(r'Newtown Middle School', chosen['name'], re.I):
        city = 'Newtown'
    # The organization's dated detail pages are Philadelphia-area performances.
    # Use its home city only when the page does not identify a different city.
    city = city or 'Philadelphia'
    return chosen['name'], city


def parse_item(item):
    title = clean_text(item.get('title', {}).get('rendered'))
    url = item.get('link', '')
    blocks, full_text = content_blocks(item.get('content', {}).get('rendered', ''))
    if not title or not url:
        return []

    date_section = section_text(full_text, 'Date & Time', ['Artists', 'Program', 'Venue'])
    if item.get('_crawler_content_type') == 'pages' and not date_section:
        return []
    # The 2021 template has no Date & Time label, but places occurrences first.
    date_source = date_section or full_text
    matches = list(DATE_RE.finditer(date_source))
    if not matches:
        return []

    options = venue_options(blocks, full_text)
    description = section_text(full_text, 'Artists', ['Venue', 'Duration'])
    if not description:
        description = section_text(full_text, 'About This Performance', ['Venue', 'Duration'])
    description = clean_text(description) or None

    records = []
    for index, match in enumerate(matches):
        event_date = parse_date(match)
        time_from = parse_time(match.group('time'))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(date_source)
        venue, city = match_venue(match, date_source[match.end():end], options, index, len(matches))
        if not event_date or not time_from or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def api_items(session, content_type):
    items = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{content_type}',
            params={'per_page': 100, 'page': page, 'status': 'publish'},
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        for item in batch:
            item['_crawler_content_type'] = content_type
        items.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return items


def hydrate_project(item):
    response = requests.get(item['link'], headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('#main-content')
    if main:
        item['content']['rendered'] = str(main)
    return item


def hydrate_items(items):
    hydrated = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(hydrate_project, item) for item in items]
        for future in as_completed(futures):
            try:
                hydrated.append(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to retrieve concert detail',
                    event='crawler_detail_error',
                    level='warning',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return hydrated


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for content_type in ('pages', 'project'):
        try:
            items = api_items(session, content_type)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to retrieve WordPress event content',
                event='crawler_api_error',
                level='error',
                url=f'{API_URL}/{content_type}',
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        if content_type == 'pages':
            items = [
                item for item in items
                if re.search(r'Date\s*&(?:amp;)?\s*Time', item.get('content', {}).get('rendered', ''), re.I)
            ]
        items = hydrate_items(items)
        for item in items:
            records.extend(parse_item(item))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No dated concert detail pages found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class ChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chamberorchestra_org',
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
        return scrape_concerts()


def main():
    ChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
