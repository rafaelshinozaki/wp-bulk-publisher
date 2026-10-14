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

## Design decisions

- **Validate everything before calling the API.** Bad rows never reach the client's site, and `--dry-run` gives the full list of problems without any risk.
- **Scheduled or draft, never `publish`.** Rows go out as `future` (scheduled for `publish_date`) or `draft` with `--status draft`. Nothing goes live straight from a script.
- **Dates are sent as `date_gmt` in UTC.** The schedule doesn't depend on the site's timezone setting. A date without a timezone is treated as UTC, and a date with an offset (`-03:00`) is converted.
- **Only transient errors are retried.** 429, 500, 502, 503, 504, timeouts and connection errors are retried up to 4 times with exponential backoff (1s, 2s, 4s), using `Retry-After` on a 429 when it's longer. Any other 4xx fails right away: a 400, 401 or 403 won't fix itself, and retrying only adds load on the client's server.
- **Writes are paced at 1 request per second.** Security plugins and WAFs (Wordfence, Cloudflare) tend to block bursts of POSTs, and shared hosting can slow down for real visitors. GETs are not paced because they are cheap, and category IDs are looked up once and cached.
- **No duplicate posts.** Before creating a post the script checks if the slug already exists (`status=any`, so drafts and scheduled posts count), which makes re-running a partially failed batch safe. And if a POST times out, the slug is checked again before retrying, because the request may have worked on the server. Without this, WordPress would accept the second POST and create `<slug>-2`.
- **The Yoast meta is checked after each create.** WordPress silently drops meta keys that aren't registered for REST and still returns 201, so the script checks that the keys came back non-empty. It doesn't compare exact values, because WordPress sanitizes the text on save.
- **One row can't kill the run.** Each row has its own `try/except`, so an API error or an unexpected exception goes to the report and the script moves on.
- **Exit code 1 when any row fails at the API level,** so a cron job, pipeline or CI step can alert on it. Validation failures don't change the exit code, since they are content problems, not system problems.
- **Credentials only from environment variables,** never from command-line arguments (they would show up in the shell history and process list), and never over plain HTTP except on localhost.

## Limitations

- **Featured images and the Yoast primary category are not set.** The CSV doesn't have those columns. Supporting images would mean uploading each one to `/wp/v2/media` first and passing the returned ID as `featured_media`.
- **Small race window.** Between checking the slug and creating the post, another process could create the same slug. Fine for a single scheduled run; with several runners in parallel, a lock or a run ledger (see below) would be needed.
- **Sequential.** About one post per second, so 100 rows take a couple of minutes. That's on purpose to protect the client's site, not a performance limit of the code.
- **Edits are not synced.** If a row changes after its post was created, the script skips it as `skipped_exists` instead of updating the post.
- **The Yoast meta keys are hardcoded.** RankMath or another SEO plugin would need different keys (see next steps).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The unit tests (`tests/test_wp_bulk_publish.py`) cover every validation rule, the inclusive length limits, date and timezone handling, duplicate slugs inside the CSV, the Yoast meta check, and the HTTP client: 5xx retried until success, 4xx not retried, giving up after the maximum number of retries, `Retry-After`, a timed-out POST that already created the post (no second POST is sent), and a failed row not stopping the run. The HTTP tests use a scripted fake session, so they don't need a server and run in under a second.

## Testing locally

Besides the unit tests, there are three ways to run the script end to end, from lightest to most realistic.

### 1. Mock server (no WordPress)

`mock_wp_server.py` simulates the endpoints the script uses. The sample CSV has rows that trigger every validation rule, plus a row where the mock returns 503 twice before succeeding, a row it rejects with 400, and a row whose slug already exists. This is the easiest way to see the retry and error paths.

```bash
python mock_wp_server.py &

WP_URL=http://127.0.0.1:8765 WP_USER=bot WP_APP_PASSWORD="app pass word" \
  python wp_bulk_publish.py articles.csv --report report.json
```

Expected result: 4 posted, 6 validation_failed, 1 api_failed, 1 skipped_exists (see `sample_report.json`).

### 2. Real WordPress without Docker (PHP + SQLite)

`local-wp/setup.sh` downloads WordPress, WP-CLI and the official SQLite database plugin, installs WordPress, creates the categories, an Editor user and an Application Password, and copies the mu-plugin. It also tries to install Yoast from wordpress.org. Requires git, curl and PHP 8.1+ with `pdo_sqlite`.

```bash
./local-wp/setup.sh
php -S 127.0.0.1:8080 -t .local-wp/wordpress .local-wp/router.php
# in another terminal, use the exports printed by setup.sh
python wp_bulk_publish.py articles.csv --report report.json
```

Expected result: 6 posted as scheduled posts with the Yoast meta saved, 6 validation_failed (see `sample_report_real_wp.json`). The rows that fail on the mock (503, 400, existing slug) are created normally here, since a real WordPress doesn't force those errors. Running the script a second time returns `skipped_exists` for all 6, and removing the mu-plugin makes the report show the "Yoast meta not persisted" warning, because WordPress drops the unregistered meta and still returns 201.

### 3. Real WordPress with Docker (MySQL + Apache + Yoast)

```bash
docker compose up -d
docker compose logs -f setup   # prints the Application Password and exports when it's done
python wp_bulk_publish.py articles.csv --report report.json
```

WordPress runs at http://localhost:8080 (admin / admin). `docker compose down -v` removes everything.

This stack installs WordPress 6.9, MySQL 8 and the current Yoast SEO release (Yoast now requires WordPress 6.9+). Tested on Windows 11 with Docker Desktop: the 6 valid rows are created as scheduled posts, Yoast picks up the title and meta description from the meta fields (visible in `yoast_head_json` on `GET /wp/v2/posts/<id>?context=edit`) and generates the Article schema on its own, and a second run returns `skipped_exists` for all 6.

On Windows (PowerShell), the env vars are set like this:

```powershell
$env:WP_URL = "http://localhost:8080"
$env:WP_USER = "automation-bot"
$env:WP_APP_PASSWORD = "<password printed by the setup container>"
python wp_bulk_publish.py articles.csv --report report.json
```

`.gitattributes` forces LF line endings on the shell scripts, so a Windows checkout doesn't break them inside the Linux containers.

## Files

| File | Description |
|---|---|
| `wp_bulk_publish.py` | Main script |
| `articles.csv` | Sample input |
| `yoast-rest-meta.php` | mu-plugin that makes Yoast fields writable via REST |
| `mock_wp_server.py` | Local mock of the WordPress REST API |
| `tests/test_wp_bulk_publish.py` | Unit tests (pytest) |
| `sample_report.json` | Report generated from the sample CSV against the mock |
| `sample_report_real_wp.json` | Report generated from the sample CSV against a real WordPress (PHP + SQLite setup) |
| `local-wp/setup.sh` | Real WordPress on PHP + SQLite, no Docker |
| `docker-compose.yml`, `docker/setup.sh` | Real WordPress with MySQL and Yoast in Docker |

## Next steps for production use

- Credentials in a secrets manager, one Application Password per client
- Per-client config file (URL, categories, rate limit, Yoast or RankMath meta keys)
- Run history in Postgres keyed by client and row hash, so runs can resume and edited rows update existing posts
- Slack summary after each run and alerts when a run fails, processes zero rows or doesn't run
- Post-publish check that the live page has the right title, meta description, canonical and schema
- Run the unit tests in CI, plus an end-to-end test against the Docker WordPress
