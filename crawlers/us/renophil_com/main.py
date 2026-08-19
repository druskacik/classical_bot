import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://renophil.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Reno Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        1,
    )
}
MONTHS.update({name[:3].lower(): number for name, number in list(MONTHS.items())})


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def calendar_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for anchor in soup.select('article a.image-slide-anchor[href]'):
        url = urljoin(CALENDAR_URL, anchor['href']).split('#', 1)[0]
        if urlparse(url).netloc.lower() in {'renophil.com', 'www.renophil.com'} and url not in links:
            links.append(url)
    return links


def _iso_date(month, day, year):
    try:
        return date(int(year), MONTHS[month.lower()[:3]], int(day)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def parse_dates(text):
    text = clean_text(text)
    match = re.search(
        r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*&\s*'
        r'(?:(\w+)\s+)?(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})',
        text,
        re.I,
    )
    if match:
        month_one, day_one, month_two, day_two, year = match.groups()
        return [
            value for value in (
                _iso_date(month_one, day_one, year),
                _iso_date(month_two or month_one, day_two, year),
            ) if value
        ]

    match = re.search(
        r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})', text, re.I
    )
    if match:
        value = _iso_date(*match.groups())
        return [value] if value else []

    # The summer series omits its year but identifies its weekday and appears on
    # the current calendar. Infer only when both endpoints match that weekday.
    match = re.search(
        r'Every\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday).*?'
        r'([A-Za-z]+)\s+(\d{1,2})\s*[-–]\s*([A-Za-z]+)\s+(\d{1,2})',
        text,
        re.I | re.S,
    )
    if match:
        weekday, month_one, day_one, month_two, day_two = match.groups()
        weekday_number = ['monday', 'tuesday', 'wednesday', 'thursday',
                          'friday', 'saturday', 'sunday'].index(weekday.lower())
        today = date.today()
        for year in range(today.year - 1, today.year + 3):
            start_value = _iso_date(month_one, day_one, year)
            end_value = _iso_date(month_two, day_two, year)
            if not start_value or not end_value:
                continue
            start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
            if start.weekday() == weekday_number and end.weekday() == weekday_number:
                values = []
                while start <= end:
                    values.append(start.isoformat())
                    start += timedelta(days=7)
                return values

    dated_lines = re.findall(
        r'(?m)^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\n'
        r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?$', text, re.I
    )
    if dated_lines:
        today = date.today()
        for year in range(today.year - 1, today.year + 3):
            values = [_iso_date(month, day, year) for _, month, day in dated_lines]
            if all(values) and all(
                date.fromisoformat(value).strftime('%A').lower() == weekday.lower()
                for value, (weekday, _, _) in zip(values, dated_lines)
            ):
                return values
    return []


def parse_times(lines):
    values = []
    for line in lines[:25]:
        for match in re.finditer(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?', line, re.I):
            hour, minute, meridiem = match.groups()
            hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
            value = f'{hour:02d}:{int(minute or 0):02d}'
            context = line[max(0, match.start() - 18):match.end() + 18].lower()
            after = line[match.end():match.end() + 18].lower()
            before = line[max(0, match.start() - 18):match.start()].lower()
            is_concert = 'concert' in after or 'concert' in before
            if 'concert' in line.lower() and not is_concert:
                continue
            if not is_concert and any(
                word in context for word in ('door', 'registration', 'activit', 'rehearsal', 'sectional')
            ):
                continue
            if value not in values:
                values.append(value)
    return values


def parse_venue_and_city(lines):
    venue = ''
    city = ''
    for index, line in enumerate(lines[:30]):
        if line.lower().rstrip(':') == 'location' and index + 1 < len(lines):
            venue = lines[index + 1].rstrip(',')
        if re.search(r'\b(Reno|Sparks)\s*,?\s*NV\b', line, re.I):
            city = re.search(r'\b(Reno|Sparks)\b', line, re.I).group(1)
            if not venue and index:
                previous = lines[index - 1].rstrip(',')
                if not re.match(r'^\d', previous):
                    venue = previous

    if not venue:
        for line in lines[:20]:
            if re.search(
                r'\b(Center|Centre|Theatre|Theater|Hall|Plaza|School|Club|Resort|Park|Museum)\b',
                line,
                re.I,
            ) and not re.search(r'concerts?|series|activities', line, re.I):
                venue = line.rstrip(',')
                break
    return clean_text(venue), city or 'Reno'


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article')
    if not article:
        return []
    description = clean_text(article.get_text('\n', strip=True))
    lines = [line for line in description.splitlines() if line]
    dates = parse_dates(description)
    venue, city = parse_venue_and_city(lines)
    title_tag = soup.select_one('meta[property="og:title"]')
    title = clean_text(title_tag.get('content')) if title_tag else ''
    title = re.split(r'\s+[|—]\s+', title, maxsplit=1)[0].strip()
    if not title:
        heading = article.select_one('h1, h2')
        title = clean_text(heading.get_text(' ', strip=True)) if heading else ''
    if not title or not dates or not venue or not city:
        return []

    times = parse_times(lines)
    schedule = []
    weekday_lines = {}
    for line in lines[:25]:
        match = re.match(r'(MON|TUE|WED|THU|FRI|SAT|SUN)\s*[-–]\s*(.*)', line, re.I)
        if match:
            weekday_lines[match.group(1).upper()] = parse_times([match.group(2).split('| Doors')[0]])
    weekday_codes = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')
    if weekday_lines:
        for event_date in dates:
            for event_time in weekday_lines.get(weekday_codes[date.fromisoformat(event_date).weekday()], []):
                schedule.append((event_date, event_time))
    if not schedule:
        schedule = [
            (event_date, times[index] if index < len(times) else (times[0] if len(times) == 1 else None))
            for index, event_date in enumerate(dates)
        ]

    records = []
    for event_date, time_from in schedule:
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    links = calendar_links(response.text)
    records = []
    for url in links:
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            parsed = parse_event(detail.text, detail.url)
            if not parsed:
                log_message('Event page skipped', event='crawler_event_skipped', level='warning', url=url)
            records.extend(parsed)
        except requests.RequestException as error:
            log_message(
                'Event request failed',
                event='crawler_event_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RenoPhilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='renophil_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    RenoPhilComCrawler().run()


if __name__ == '__main__':
    main()
