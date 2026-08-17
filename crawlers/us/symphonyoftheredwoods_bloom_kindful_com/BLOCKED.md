<!-- crawler-factory-metadata
{"url":"https://kindful.com/","geographic_scope":"country","country_code":"US","reason_code":"wrong_source","attempted_at":"2026-08-17","retry_after":null}
-->

# Crawler blocked: supplied source is unrelated

The original URL is `https://kindful.com/`. It currently serves Kindful by
Bloomerang's nonprofit CRM and fundraising-software website, not a Symphony of
the Redwoods calendar or ticket feed. The likely historical tenant host,
`https://symphonyoftheredwoods-bloom.kindful.com/`, now redirects its root and
`/events` path to the same generic Kindful homepage.

A crawler cannot currently be implemented because neither the supplied URL nor
the retired tenant exposes a listing of concerts. Search-engine results show
that the tenant previously published individual Symphony of the Redwoods event
pages, including dated classical concerts, but cached snippets are not a stable
or first-party scrapeable catalogue and cannot provide complete coverage.

Investigation attempted:

- direct HTTP requests to the supplied URL, the historical tenant root, and its
  `/events` path, following and recording redirects;
- inspection of the returned HTML for embedded event data, API endpoints, and
  application state;
- discovery searches for indexed tenant pages and possible alternate Kindful
  tenant hostnames;
- requests to a representative historical `/e/...` event route, which did not
  expose a usable live event catalogue.

The requested Playwright MCP was not available in this execution environment.
Browser-style HTTP and rendered search inspection nevertheless establish that
the live destination is the unrelated generic Kindful product site, so browser
network capture would not recover the retired organization's catalogue.

Implementation would be unblocked by a current first-party Symphony of the
Redwoods events URL or a restored Kindful/Bloom tenant whose listing or API
exposes concrete event dates, venues, and cities. The resolved organization is
US-based, so its geographic scope remains country-level `US`.
