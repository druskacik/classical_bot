import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://phillniblock.com/'
SOURCE = 'Phill Niblock'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
CATEGORY_IDS = '4,5,7'  # News, Experimental Intermedia, Live

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        1,
    )
}

# The site covers Phill Niblock performances around the world.  These names are
# deliberately conservative: an unknown place is skipped rather than uploaded
# with a guessed country.
PLACES = {
    'Amsterdam': 'NL', 'Athens': 'GR', 'Barcelona': 'ES', 'Basel': 'CH',
    'Belgrade': 'RS', 'Berlin': 'DE', 'Bradford': 'GB', 'Brighton': 'GB',
    'Brooklyn': 'US', 'Brussels': 'BE', 'Copenhagen': 'DK', 'Düsseldorf': 'DE',
    'Gent': 'BE', 'Ghent': 'BE', 'Glasgow': 'GB', 'Hamburg': 'DE',
    'Kyoto': 'JP', 'Leiden': 'NL', 'Lisbon': 'PT', 'London': 'GB',
    'Luxembourg': 'LU', 'Montréal': 'CA', 'Montreal': 'CA', 'Nantes': 'FR',
    'New York': 'US', 'NYC': 'US', 'Ogaki': 'JP', 'Osaka': 'JP',
    'Ostrava': 'CZ', 'Paris': 'FR', 'Porto': 'PT', 'Prague': 'CZ',
    'Rome': 'IT', 'Sendai': 'JP', 'Swansea': 'GB', 'Tokyo': 'JP',
    'Torino': 'IT', 'Turin': 'IT', 'Vienna': 'AT', 'Wien': 'AT',
}


def clean_text(value):
    soup = BeautifulSoup(html.unescape(value or ''), 'html.parser')
    return re.sub(r'[ \t]+', ' ', soup.get_text('\n')).strip()


def parse_dates(text):
    """Return explicit English-language calendar dates, including joined days."""
    dates = []
    pattern = re.compile(
        r'(?i)(?:(\d{1,2})(?:st|nd|rd|th)?\s*(?:&|and|–|-)\s*)?'
        r'(\d{1,2})(?:st|nd|rd|th)?\s+'
        r'(' + '|'.join(MONTHS) + r')\s*,?\s*(20\d{2})'
        r'|(' + '|'.join(MONTHS) + r')\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:&|and)\s*(\d{1,2})(?:st|nd|rd|th)?)?'
        r'\s*,?\s*(20\d{2})'
    )
    for match in pattern.finditer(text):
        if match.group(3):
            days = [match.group(1), match.group(2)]
            month, year = match.group(3), match.group(4)
        else:
            days = [match.group(6), match.group(7)]
            month, year = match.group(5), match.group(8)
        for day in filter(None, days):
            try:
                value = datetime(int(year), MONTHS[month.lower()], int(day)).date().isoformat()
            except ValueError:
                continue
            if value not in dates:
                dates.append(value)
    return dates


def parse_title_dates(title):
    # For an exhibition-style range, the first day is the concrete opening;
    # the closing date is not a second performance.
    range_match = re.search(
        r'(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+('
        + '|'.join(MONTHS)
        + r')\s*[–-]\s*\d{1,2}(?:st|nd|rd|th)?\s+('
        + '|'.join(MONTHS)
        + r')\s*,?\s*(20\d{2})',
        title,
    )
    if range_match:
        try:
            return [
                datetime(
                    int(range_match.group(4)),
                    MONTHS[range_match.group(2).lower()],
                    int(range_match.group(1)),
                ).date().isoformat()
            ]
        except ValueError:
            return []
    return parse_dates(title)


def parse_time(text):
    performance_time = re.search(
        r'(?is)live performance.{0,120}?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b',
        text,
    )
    if performance_time:
        text = text[performance_time.start(1):]
    match = re.search(r'(?i)\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text)
    if not match:
        match = re.search(r'(?i)\b(\d{1,2})[.:](\d{2})\s*h?\b', text)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if match.group(3).lower() == 'pm' and hour != 12:
            hour += 12
        if match.group(3).lower() == 'am' and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def find_place(text):
    hits = []
    for city, country_code in PLACES.items():
        match = re.search(rf'(?i)(?<!\w){re.escape(city)}(?!\w)', text)
        if match:
            hits.append((match.start(), city, country_code))
    if not hits:
        return None
    _, city, country_code = min(hits)
    return ('New York' if city == 'NYC' else city, country_code)


def find_venue(title, text, city):
    # Most single-event posts use "Venue, City | date" or "... at Venue, City".
    venue_label = re.search(r'(?im)^\s*Venue:\s*([^\n]+)', text)
    if venue_label:
        return re.sub(r'\s+', ' ', venue_label.group(1)).strip(' @,-.')

    before_pipe = title.split('|', 1)[0].strip()
    if city.lower() in before_pipe.lower():
        candidate = re.split(rf'(?i),?\s*{re.escape(city)}\b', before_pipe)[0]
        candidate = re.sub(r'(?i)^.*?\bat\s+', '', candidate).strip(' @,-')
        if '@' in candidate:
            candidate = candidate.rsplit('@', 1)[1].strip()
        if 2 < len(candidate) <= 100 and candidate.lower() != city.lower():
            if re.match(r'(?i)^(live|phill niblock)\s+in$', candidate):
                return None
            return candidate

    patterns = (
        rf'(?i)(?:\bat|@)\s+([^\n|,]{{3,100}}?)(?:,|\s+in\s+){re.escape(city)}\b',
        rf'(?i)([A-Z][^\n|,]{{2,100}}),\s*{re.escape(city)}\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            candidate = re.sub(r'\s+', ' ', match.group(1)).strip(' @,-.')
            if candidate and candidate.lower() != city.lower():
                return candidate
    return None


def parse_post(post):
    title = clean_text(post['title']['rendered'])
    description = clean_text(post['content']['rendered'])
    combined = f'{title}\n{description}'
    if re.search(r'(?i)rsvp\s+to\s+get\s+(?:the\s+)?location', description):
        return []
    # Seasonal/tour posts contain unrelated occurrences in several places.
    # Without per-occurrence markup their dates and venues cannot be paired
    # reliably, so only parse posts whose title identifies a concrete date.
    if re.search(
        r'(?i)^(events?\b|experimental intermedia\b|news update\b|'
        r'upcoming\b|autumn\b|late spring\b|live events?\b|uk tour\b)',
        title,
    ):
        return []
    dates = parse_title_dates(title)
    if len(dates) > 1:
        described_dates = set(parse_dates(description))
        confirmed_dates = [value for value in dates if value in described_dates]
        if confirmed_dates:
            dates = confirmed_dates
    place = find_place(combined)
    if not dates or not place:
        return []
    city, country_code = place
    venue = find_venue(title, description, city)
    if not venue:
        return []
    time_from = parse_time(combined)
    return [
        {
            'title': title,
            'date': date,
            'url': post['link'],
            'time_from': time_from,
            'time_to': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
        }
        for date in dates
    ]


class PhillNiblockCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='phillniblock_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'venue'],
    )

    def scrape(self):
        records = []
        page = 1
        while True:
            log_message('Fetching WordPress event candidates', event='crawler_url_fetch', url=API_URL, page=page)
            response = requests.get(
                API_URL,
                params={'categories': CATEGORY_IDS, 'per_page': 100, 'page': page},
                timeout=30,
            )
            response.raise_for_status()
            posts = response.json()
            for post in posts:
                # Releases can also be tagged News; they are not event candidates.
                if 6 not in post.get('categories', []):
                    records.extend(parse_post(post))
            if page >= int(response.headers.get('X-WP-TotalPages', '1')):
                break
            page += 1
        return records


def main():
    PhillNiblockCrawler().run()


if __name__ == '__main__':
    main()
