# Token usage

The **Usage** button opens a separate window with token totals, a stacked
timeline, model distribution, a sortable model table, and CSV exports. Filters
cover model/provider, inclusive UTC dates and hourly, daily or weekly grouping.
Weekly buckets start on the selected range's first day. Automatic grouping uses
hours for one or two days, days for up to 120 days, and weeks for longer ranges.
The window refreshes every 30 seconds while visible; live refresh can be disabled.

Users see their own activity. Admins can select **Whole server** to aggregate all
users. Both the page and `/api/usage` require the existing account session;
server-wide queries require an admin role. Reports are not cached.

## Accounting and persistence

`UsageStore` saves each observed completed model response immediately in SQLite,
including nested agents, provider-reported model names, cached input, and
reasoning output. Cached input is a subset of input; reasoning output is a subset
of output. They are never added a second time to the total. Model response counts
are separate from conversation turn counts. Missing token reports are excluded.
Routing, policy checks and other calls outside agent response streams are not
included. This is observed token usage, not a provider billing statement.

Model names come from responses, with a single configured model as a fallback.
Provider attribution uses matching configured models; ambiguous provider names
are left unrecorded. A fallback chain without a reported model remains unknown.
Events contain no prompts, response text, API keys or conversation content.

The database is `DATA_DIR/usage.db` with accounts, or `LOGS_DIR/usage.db` in single
user mode. WAL and immediate per-response writes preserve completed response
counts across server restarts. Existing daily totals from `accounts.db` are
imported once atomically at first startup. They appear as **Unknown model**,
with no invented response count, cache details or hour. Historical daily totals
remain in summaries and daily/weekly charts; hourly charts show only detailed
responses and display a coverage note. Existing daily account limits continue
to use the account store.

## API

`GET /api/usage?start=2026-10-01&end=2026-10-03&bucket=day&scope=me`

`start` and `end` are inclusive ISO dates. The default is the last seven UTC days.
`bucket` accepts `auto`, `hour`, `day`, `week`. `scope` accepts `me` or `all`.
`model` optionally contains a JSON pair `[provider, model]`, encoded as a query
parameter. The response contains `summary`, `models`, `model_options`, zero-filled
`series`, and `coverage`. Ranges are limited to ten years and 1,000 chart points.
