# wp-bulk-publisher

Python script that reads articles from a CSV, validates them and publishes the valid ones to WordPress through the REST API, with Yoast SEO fields, rate limiting, retries and a per-row report.

## What it does

1. Reads `articles.csv` (columns: `title`, `slug`, `meta_title`, `meta_description`, `body_html`, `primary_keyword`, `publish_date`, `category`).
2. Validates each row:
   - all required fields present
   - slug only has lowercase letters, numbers and single hyphens
   - `title` and `meta_title` up to 60 characters
   - `meta_description` up to 160 characters
   - `publish_date` is valid ISO 8601 and in the future
   - `category` is in the allow-list (`blog`, `guides`, `news`)
   - no duplicate slugs inside the same file
3. Publishes valid rows with `POST /wp-json/wp/v2/posts` using Application Password auth, as scheduled posts (`status=future`) or drafts.
4. Sends the Yoast title, meta description and focus keyword in the `meta` field and checks that WordPress actually saved them.
5. Paces write requests (1 per second), retries 429, 5xx and timeouts with exponential backoff (respecting `Retry-After`), and keeps going when a single row fails.
6. Skips posts whose slug already exists, and checks the slug again before retrying a POST so a request that timed out but succeeded on the server doesn't create a duplicate.
7. Writes a JSON report with the status of every row.

## Report statuses

| Status | Meaning |
|---|---|
| `posted` | Created on WordPress (includes post ID and link) |
| `validation_failed` | Row rejected before calling the API, with the list of reasons |
| `api_failed` | WordPress returned an error, with status code and message |
| `skipped_exists` | A post with the same slug already exists |
| `valid_dry_run` | Row is valid (only in `--dry-run` mode) |

A `posted` row can also include a `warning` if the Yoast fields were not saved. See `sample_report.json` for a real example.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Yoast fields

Yoast's REST API is read-only, and WordPress doesn't accept writes to protected meta keys (the ones starting with `_`) unless they are registered. Copy `yoast-rest-meta.php` to `wp-content/mu-plugins/` on the site so `_yoast_wpseo_title`, `_yoast_wpseo_metadesc` and `_yoast_wpseo_focuskw` can be written through `/wp/v2/posts`. Without it the posts are still created, but the report flags them with a warning.

### Credentials

Create an Application Password for a dedicated user (Users > Profile > Application Passwords) with the Editor role, and set it as environment variables:

```bash
export WP_URL=https://client-site.com
export WP_USER=automation-bot
export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
```

The script refuses to send credentials over plain HTTP (except localhost).

## Usage

```bash
# validate only, no API calls
python wp_bulk_publish.py articles.csv --dry-run

# schedule posts for their publish_date
python wp_bulk_publish.py articles.csv --report report.json

# create as drafts for review instead
python wp_bulk_publish.py articles.csv --report report.json --status draft
```

The exit code is `1` if any row failed at the API level, so a scheduler or CI job can detect it.

## Testing locally with the mock server

`mock_wp_server.py` simulates the endpoints the script uses. The sample CSV has rows that trigger every validation rule, plus a row where the server returns 503 twice before succeeding, a row the server rejects with 400, and a row whose slug already exists.

```bash
python mock_wp_server.py &

WP_URL=http://127.0.0.1:8765 WP_USER=bot WP_APP_PASSWORD="app pass word" \
  python wp_bulk_publish.py articles.csv --report report.json
```

Expected result: 4 posted, 6 validation_failed, 1 api_failed, 1 skipped_exists.

## Files

| File | Description |
|---|---|
| `wp_bulk_publish.py` | Main script |
| `articles.csv` | Sample input |
| `yoast-rest-meta.php` | mu-plugin that makes Yoast fields writable via REST |
| `mock_wp_server.py` | Local mock of the WordPress REST API |
| `sample_report.json` | Report generated from the sample CSV against the mock |

## Next steps for production use

- Credentials in a secrets manager, one Application Password per client
- Per-client config file (URL, categories, rate limit, Yoast or RankMath meta keys)
- Run history in Postgres keyed by client and row hash, so runs can resume and edited rows update existing posts
- Slack summary after each run and alerts when a run fails, processes zero rows or doesn't run
- Post-publish check that the live page has the right title, meta description, canonical and schema
- Unit tests for validation and an integration test against the mock in CI
