<!-- crawler-factory-metadata
{"url":"https://www.eventfinda.co.nz/","geographic_scope":"country","country_code":"NZ","reason_code":"access_blocked","attempted_at":"2026-08-14","retry_after":"2026-09-13"}
-->

# Eventfinda New Zealand crawler blocked

## Original URL

https://www.eventfinda.co.nz/

## Why a crawler cannot currently be implemented

Eventfinda is a New Zealand-wide, mixed-event source. Its public pages are
protected by an AWS WAF JavaScript challenge. Production-compatible HTTP
requests return HTTP 202 with `x-amzn-waf-action: challenge` and no usable
listing or event HTML. A browser can complete the challenge, but this repository
does not include a browser automation dependency, and the task does not permit
changing shared dependencies.

Eventfinda advertises a structured Developer API, but access requires an
Eventfinda API account/application. No unauthenticated event-data endpoint was
exposed by the public pages' network traffic.

## Approaches attempted

- Inspected the site with Playwright, including network requests on the home,
  category, paginated category, and representative detail pages. No internal
  event-data API request was present; content was delivered in server-rendered
  HTML after the browser completed the WAF challenge.
- Verified the national first-party category feeds
  `/concerts-gig-guide/events/new-zealand` and
  `/arts/events/new-zealand`. Their `/page/N` identifiers persist across
  pagination (including page 2). The site exposes no Classical subcategory.
- Inspected adjacent category taxonomy. Concerts & Gig Guide contains genre
  filters such as Music Festivals, Variety Concerts, Jazz, World, Pop, and many
  others, while Performing Arts is a separate mixed category. No stable filter
  or combination comprehensively isolates classical music, opera, ballet,
  contemporary art music, and eligible crossover events.
- Inspected representative detail pages. They contain schema.org JSON-LD with
  one object per dated session, venue/location data, and a full HTML event
  description. For example, the KBB Music Festival page exposed concrete daily
  sessions, Holy Trinity Cathedral, Auckland, and orchestral programme details.
- Tested direct access using Python `requests`, command-line `curl`, and the
  repository's `curl_cffi` with Chrome and Safari impersonation. Each received
  the AWS WAF challenge rather than scrapeable page content.
- Opened the first-party Developer API entry point. It is an application/signup
  flow rather than an anonymously usable API.
- Tested the apparent `/past` category suffix; it redirects to the upcoming
  category, so no stable public archive feed was found.

Because this is a mixed source with no sufficiently comprehensive classical
filter, a future crawler should combine the national Concerts & Gig Guide and
Performing Arts candidate feeds and use `upload_target="potential"`. This has
substantial expected contamination (popular concerts, theatre, comedy, and
ordinary dance), but avoids systematically omitting eligible opera, ballet,
orchestral, family, and crossover events. Detail-page session objects should be
emitted as individual occurrences rather than as festival overview records.

## What would unblock implementation

Any one of the following would unblock a production crawler:

- Eventfinda Developer API credentials made available to the crawler runtime;
- a documented, unauthenticated event API or export that is not WAF-blocked;
- permission to add and deploy a supported browser automation dependency; or
- an Eventfinda allow-list or WAF exemption for the production crawler.
