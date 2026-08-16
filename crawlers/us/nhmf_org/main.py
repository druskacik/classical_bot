import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nhmf.org/'
FESTIVAL_URL = f'{SOURCE_URL}2026-festival/'
SOURCE = 'New Hampshire Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# Each first-party page anchor marks the event block that follows it.  The
# location names are kept here because some outreach listings show an address
# on a separate line while the main series combines venue and city in one line.
EVENT_LOCATIONS = {
    'WV': ('Waterville Valley Family Carnival', 'Waterville Valley'),
    'Week_1_Ch': ('PSU Silver Center for the Arts', 'Plymouth'),
    'PLY': ('Plymouth Town Hall', 'Plymouth'),
    'OWL': ("Owl's Nest Resort Outdoor Entertainment Stage", 'Thornton'),
    'LFP': ('Local Foods Plymouth', 'Plymouth'),
    'Week_1': ('PSU Silver Center for the Arts', 'Plymouth'),
    'Cafe_1': ('Cafe Monte Alto', 'Plymouth'),
    'Hermit': ('Hermit Woods Winery', 'Meredith'),
    'Week_2_Ch': ('PSU Silver Center for the Arts', 'Plymouth'),
    'Week_2': ('PSU Silver Center for the Arts', 'Plymouth'),
    'RATT': ('West Rattlesnake Mountain', 'Holderness'),
    'Cafe_2': ('Cafe Monte Alto', 'Plymouth'),
    'enchanted': ('Van Horn Estate', 'Holderness'),
    'Week_3_Ch': ('PSU Silver Center for the Arts', 'Plymouth'),
    'Week_3': ('PSU Silver Center for the Arts', 'Plymouth'),
    'Cafe_3': ('Cafe Monte Alto', 'Plymouth'),
    'Starr': ('Starr King Fellowship', 'Plymouth'),
    'Taylor': ('Taylor Community, Woodside Building', 'Laconia'),
    'Week_4_Ch': ('PSU Silver Center for the Arts', 'Plymouth'),
    'Week_4': ('PSU Silver Center for the Arts', 'Plymouth'),
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    )
    if name
}

DATE_RE = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),\s*(20\d{2})\b', re.I
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?\b', re.I)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(match):
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(match):
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def event_blocks(soup):
    root = soup.select_one('.elementor-114')
    if root is None:
        raise ValueError('Could not find the festival page content')

    current_id = None
    for child in root.find_all(recursive=False):
        anchor = child.select_one('.elementor-menu-anchor[id]')
        if anchor is not None:
            current_id = anchor.get('id')
            continue
        if current_id in EVENT_LOCATIONS:
            text = clean_text(child)
            if DATE_RE.search(text):
                yield current_id, text
                current_id = None


def title_from_text(text, date_match):
    all_lines = text.splitlines()
    date_line_index = next(
        (index for index, line in enumerate(all_lines) if date_match.group(0) in line), None
    )
    date_line = all_lines[date_line_index] if date_line_index is not None else ''
    suffix = re.sub(
        r'^.*?' + re.escape(date_match.group(0)) + r'\s*[–—-]\s*', '', date_line
    ).strip()
    if suffix and suffix != date_line:
        return suffix

    if date_line_index is not None and re.search(r'[–—-]\s*$', date_line):
        following = [line.strip() for line in all_lines[date_line_index + 1:] if line.strip()]
        if following:
            return following[0]

    lines = [line.strip() for line in text[:date_match.start()].splitlines() if line.strip()]
    return lines[-1] if lines else ''


def times_from_text(anchor_id, text, date_match):
    date_line_end = text.find('\n', date_match.end())
    leading = text[date_match.start():date_line_end if date_line_end >= 0 else len(text)]
    matches = list(TIME_RE.finditer(leading))
    if not matches:
        following = text[date_match.end():date_match.end() + 100]
        matches = list(TIME_RE.finditer(following))

    # This fundraiser lists cocktails and dinner before naming the concert's
    # actual 7:30 performance time later in its prose.
    if anchor_id == 'enchanted':
        performance = re.search(r'continues\s+at\s*' + TIME_RE.pattern, text, re.I)
        return [parse_time(performance)] if performance else []
    if anchor_id == 'PLY':
        return [parse_time(matches[0])] if matches else []
    return [parse_time(match) for match in matches]


def parse_block(anchor_id, text):
    date_match = DATE_RE.search(text)
    if date_match is None:
        return []
    event_date = parse_date(date_match)
    title = title_from_text(text, date_match)
    venue, city = EVENT_LOCATIONS[anchor_id]
    if not event_date or not title or not venue or not city:
        return []

    times = times_from_text(anchor_id, text, date_match) or [None]
    url = f'{FESTIVAL_URL}#{anchor_id}'
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_time in dict.fromkeys(times)
    ]


class NhmfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nhmf_org',
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
        try:
            response = requests.get(FESTIVAL_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch NHMF festival page',
                event='crawler_fetch_failed',
                level='error',
                url=FESTIVAL_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for anchor_id, text in event_blocks(soup):
            records.extend(parse_block(anchor_id, text))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    NhmfOrgCrawler().run()


if __name__ == '__main__':
    main()
