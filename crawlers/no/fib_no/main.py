import re
from datetime import date, datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fib.no/'
SOURCE = 'Festspillene i Bergen'
API_URL = 'https://api.storyblok.com/v2/cdn/stories'
API_TOKEN = 'AJugKgiNTTmECve9cFnk7Qtt'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def rich_text(document):
    """Render the textual part of a Storyblok rich-text document."""
    lines = []

    def visit(node):
        if not isinstance(node, dict):
            return
        if node.get('type') == 'hard_break':
            lines.append('\n')
        elif isinstance(node.get('text'), str):
            lines.append(node['text'])
        for child in node.get('content') or []:
            visit(child)
        if node.get('type') in {'paragraph', 'heading', 'list_item'}:
            lines.append('\n')

    visit(document)
    return clean_text(''.join(lines))


def production_description(production):
    content = production.get('content') or {}
    parts = [content.get('excerpt'), content.get('SyncDescription')]
    for block in content.get('body') or []:
        component = block.get('component')
        if component == 'LeadBlock':
            parts.append(block.get('lead'))
        elif component == 'RichText':
            parts.append(rich_text(block.get('content')))

    result = []
    for part in parts:
        value = clean_text(part)
        if value and value not in result:
            result.append(value)
    return '\n\n'.join(result) or None


def local_occurrence(value, slug=''):
    # The stable occurrence slug contains the public local time. This matters
    # because older imported rows mix local values with UTC-normalized values
    # in SyncEventStartTime, while their slugs remain consistent.
    match = re.search(r'-(20\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})$', slug)
    if match:
        try:
            event_date = date(*map(int, match.group(1, 2, 3))).isoformat()
            hour, minute = map(int, match.group(4, 5))
            if hour < 24 and minute < 60:
                return event_date, f'{hour:02d}:{minute:02d}'
        except ValueError:
            pass
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def norwegian_production(event, productions):
    choices = [
        productions.get(uuid)
        for uuid in event.get('content', {}).get('SyncProduction') or []
    ]
    return next(
        (story for story in choices if story and story.get('full_slug', '').startswith('no/program/')),
        None,
    )


def production_venue_uuid(production, event=None):
    content = production.get('content') or {}
    if content.get('SyncVenue'):
        return content['SyncVenue']
    for block in content.get('body') or []:
        if block.get('component') == 'ProductionDetails' and block.get('venue'):
            return block['venue']
    return (event or {}).get('content', {}).get('SyncVenue')


def venue_details(production, event, venues):
    venue = venues.get(production_venue_uuid(production, event))
    if not venue:
        return None
    content = venue.get('content') or {}
    name = clean_text(content.get('SyncName') or content.get('name') or venue.get('name'))
    city = clean_text(content.get('SyncCity'))
    if not city:
        # The festival's venue pages and category records establish these as
        # Bergen-area venues. Valestrand is the one venue outside Bergen city.
        city = 'Valestrandsfossen' if name.casefold() == 'valestrand' else 'Bergen'
    return (name, city) if name else None


class FibNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fib_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, **params):
        params.update(version='published', language='no', token=API_TOKEN)
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        return response

    def _all_events(self, session):
        common = {
            'starts_with': 'data/arrangement',
            'filter_query[SyncPrivateEvent][is]': 'false',
            'sort_by': 'content.SyncEventStartTime:asc',
            'per_page': 100,
        }
        events = []
        page = 1
        while True:
            response = self._get(session, page=page, **common)
            stories = response.json().get('stories') or []
            events.extend(stories)
            total = int(response.headers.get('total', len(events)))
            if not stories or len(events) >= total:
                break
            page += 1
        return events

    def _stories_by_uuid(self, session, uuids):
        result = {}
        unique = list(dict.fromkeys(uuid for uuid in uuids if uuid))
        for offset in range(0, len(unique), 50):
            response = self._get(
                session,
                by_uuids=','.join(unique[offset:offset + 50]),
                per_page=50,
            )
            for story in response.json().get('stories') or []:
                result[story['uuid']] = story
        return result

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = self._all_events(session)
        productions = self._stories_by_uuid(
            session,
            [uuid for event in events for uuid in event.get('content', {}).get('SyncProduction') or []],
        )
        norwegian = {
            story['uuid']: story
            for story in productions.values()
            if story.get('full_slug', '').startswith('no/program/')
        }
        venues = self._stories_by_uuid(
            session,
            [production_venue_uuid(story) for story in norwegian.values()]
            + [event.get('content', {}).get('SyncVenue') for event in events],
        )

        records = []
        for event in events:
            content = event.get('content') or {}
            occurrence = local_occurrence(
                content.get('SyncEventStartTime'), event.get('full_slug', '')
            )
            production = norwegian_production(event, norwegian)
            venue = venue_details(production, event, venues) if production else None
            title = clean_text(content.get('SyncName') or event.get('name'))
            if not occurrence or not production or not venue or not title:
                log_message(
                    'Skipping incomplete FIB event occurrence',
                    event='crawler_item_skipped',
                    level='warning',
                    url=event.get('full_slug'),
                    error_type='IncompleteEvent',
                    error_message='Missing date, Norwegian production, venue, city, or title',
                )
                continue
            slug = production['full_slug'].removeprefix('no/')
            records.append({
                'title': title,
                'date': occurrence[0],
                'url': SOURCE_URL + slug,
                'time_from': occurrence[1],
                'venue': venue[0],
                'city': venue[1],
                'description': production_description(production),
            })

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'], item['title'], item['venue']
        ))


def main():
    return FibNoCrawler().run()


if __name__ == '__main__':
    main()
