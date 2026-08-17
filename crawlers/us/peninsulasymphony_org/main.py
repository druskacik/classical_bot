import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://peninsulasymphony.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Peninsula Symphony'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
MONTHS = {
    name.lower(): number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'), 1
    )
}
MONTHS.update({name[:3].lower(): number for name, number in list(MONTHS.items())})
OCCURRENCE_RE = re.compile(
    r'^(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|MON|TUE|WED|THU|FRI|SAT|SUN)'
    r'\s*[/,]\s*([A-Za-z]+)\s+(\d{1,2})(?:,\s*(20\d{2}))?\s*'
    r'(?:-|/|at)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', re.IGNORECASE
)
HEADING_RE = re.compile(
    r'^([A-Za-z]+)\s+(\d{1,2})\s*&\s*(\d{1,2}),\s*(20\d{2})$', re.IGNORECASE
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, parser='html.parser'):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, parser)


def city_and_venue(value):
    venue = clean_text(value).lstrip('- ').rstrip('*').strip()
    mappings = (
        ('Heritage Theatre', 'Campbell'),
        ('San Mateo Performing Arts Center', 'San Mateo'),
        ('Capuchino Performing Arts Center', 'San Bruno'),
        ('Bing Concert Hall', 'Stanford'),
        ('Los Altos United Methodist Church', 'Los Altos'),
    )
    for marker, city in mappings:
        if marker.casefold() in venue.casefold():
            return marker, city
    return None, None


def parse_time(hour, minute, meridiem):
    parsed = datetime.strptime(f'{hour}:{minute or "00"}{meridiem}', '%I:%M%p')
    return parsed.strftime('%H:%M')


def make_date(year, month_name, day):
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def page_lines(soup):
    main = soup.select_one('main')
    if not main:
        return []
    return [line for line in (clean_text(part) for part in main.get_text('\n').splitlines()) if line]


def description_between(lines, start, end):
    ignored = {'MORE INFO', 'PROGRAM'}
    parts = [line for line in lines[start:end] if line.upper() not in ignored]
    return '\n'.join(parts).strip() or None


def parse_explicit_season(url, lines):
    records = []
    for index, line in enumerate(lines):
        match = OCCURRENCE_RE.match(line)
        if not match:
            continue
        venue, city = (None, None)
        for following in lines[index + 1:index + 4]:
            venue, city = city_and_venue(following)
            if venue:
                break
        if not venue:
            continue
        title = None
        title_index = index
        heading_index = next(
            (previous for previous in range(index - 1, max(-1, index - 15), -1)
             if HEADING_RE.match(lines[previous])),
            None,
        )
        month_title_index = next(
            (previous for previous in range(index - 1, max(-1, index - 15), -1)
             if re.match(r'^[A-Z]+\s+-\s+', lines[previous])),
            None,
        )
        if heading_index is not None:
            title_index = heading_index + 1
            title_end = next(
                (position for position in range(heading_index + 1, index + 1)
                 if OCCURRENCE_RE.match(lines[position])),
                index,
            )
            title = ' '.join(lines[heading_index + 1:title_end]).strip(' -*')
        elif month_title_index is not None:
            title_index = month_title_index
            title_end = next(
                (position for position in range(month_title_index + 1, index + 1)
                 if OCCURRENCE_RE.match(lines[position])),
                index,
            )
            title = ' '.join(lines[month_title_index:title_end])
            title = re.sub(r'^[A-Z]+\s+-\s+', '', title).strip(' -*')
        for previous in range(index - 1, max(-1, index - 8), -1):
            if title:
                break
            candidate = lines[previous]
            if (candidate.upper() in {'MORE INFO', 'NORTH SERIES', 'SOUTH SERIES'}
                    or HEADING_RE.match(candidate) or OCCURRENCE_RE.match(candidate)
                    or candidate.startswith('-')):
                continue
            title = candidate.strip(' -*')
            title_index = previous
            break
        if title:
            title = re.sub(r'\s+([,;:])', r'\1', title)
        year = match.group(3)
        if not year and title:
            title_words = set(re.findall(r'[a-z0-9]+', title.casefold()))
            for candidate in lines:
                candidate_words = set(re.findall(r'[a-z0-9]+', candidate.casefold()))
                year_match = re.search(r'\b(20\d{2})\b', candidate)
                if year_match and title_words and title_words <= candidate_words:
                    year = year_match.group(1)
                    break
        event_date = make_date(year, match.group(1), match.group(2)) if year else None
        if not title or not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group(4), match.group(5), match.group(6).lower()),
            'venue': venue,
            'city': city,
            'description': description_between(lines, title_index, min(len(lines), index + 8)),
        })
    return records


def parse_current_season(url, lines):
    headings = [(index, HEADING_RE.match(line)) for index, line in enumerate(lines)]
    headings = [(index, match) for index, match in headings if match]
    records = []
    previous_heading = 0
    for heading_number, (heading_index, heading) in enumerate(headings):
        title = lines[heading_index + 1].strip(' *') if heading_index + 1 < len(lines) else ''
        next_schedule = len(lines)
        if heading_number + 1 < len(headings):
            next_heading = headings[heading_number + 1][0]
            schedule_indexes = [
                i for i in range(heading_index + 1, next_heading)
                if OCCURRENCE_RE.match(lines[i])
            ]
            if schedule_indexes:
                next_schedule = schedule_indexes[0]
        description = description_between(lines, heading_index + 2, next_schedule)
        schedules = []
        for index in range(previous_heading, heading_index):
            match = OCCURRENCE_RE.match(lines[index])
            if match and not match.group(3):
                schedules.append((index, match))
        for position, (index, match) in enumerate(schedules):
            venue = city = None
            for following in lines[index + 1:min(heading_index, index + 4)]:
                venue, city = city_and_venue(following)
                if venue:
                    break
            if not venue and position + 1 == len(schedules):
                for following in lines[index + 1:heading_index]:
                    venue, city = city_and_venue(following)
                    if venue:
                        break
            event_date = make_date(heading.group(4), match.group(1), match.group(2))
            if title and event_date and venue:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': parse_time(match.group(4), match.group(5), match.group(6).lower()),
                    'venue': venue,
                    'city': city,
                    'description': description,
                })
        previous_heading = heading_index + 1
    return records


def parse_presents(url, lines):
    text = '\n'.join(lines)
    match = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
        r'([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})\s*\n(\d{1,2})(?::(\d{2}))?\s*(AM|PM)',
        text, re.IGNORECASE,
    )
    venue, city = city_and_venue(text)
    if not match or not venue:
        return []
    return [{
        'title': 'PSO Presents',
        'date': make_date(match.group(3), match.group(1), match.group(2)),
        'url': url,
        'time_from': parse_time(match.group(4), match.group(5), match.group(6).lower()),
        'venue': venue,
        'city': city,
        'description': text,
    }]


class PeninsulaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='peninsulasymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        sitemap = get_soup(session, SITEMAP_URL, 'xml')
        urls = sorted({
            clean_text(node) for node in sitemap.select('loc')
            if re.search(r'/season-\d+$', clean_text(node))
        })
        presents_url = urljoin(SOURCE_URL, 'pso-presents-june-2026')
        if sitemap.find('loc', string=presents_url):
            urls.append(presents_url)

        records = []
        for url in urls:
            try:
                lines = page_lines(get_soup(session, url))
                if url.endswith('/season-78'):
                    page_records = parse_current_season(url, lines)
                elif url.endswith('/pso-presents-june-2026'):
                    page_records = parse_presents(url, lines)
                else:
                    page_records = parse_explicit_season(url, lines)
                records.extend(page_records)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Peninsula Symphony programme page',
                    event='crawler_page_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ))


def main():
    PeninsulaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
