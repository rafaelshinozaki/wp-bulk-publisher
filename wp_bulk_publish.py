#!/usr/bin/env python3
"""
Bulk-publish articles from a CSV to WordPress via the REST API.

- Validates every row before touching the API.
- Auth: WordPress Application Passwords (HTTP Basic over HTTPS).
- SEO: writes Yoast's title/description post meta (_yoast_wpseo_title,
  _yoast_wpseo_metadesc) through the `meta` field of /wp/v2/posts.
  NOTE: Yoast does not expose these keys as writable REST meta out of the
  box; the site needs the small mu-plugin in yoast-rest-meta.php (included).
  The script verifies the meta actually persisted and flags the row if not.
- Paces requests, retries transient failures (429/5xx/timeouts) with
  exponential backoff, and never lets one bad row kill the run.
- Idempotent: skips rows whose slug already exists on the site, and re-checks
  the slug before retrying a POST that timed out (avoids duplicate posts).
- Writes a per-row JSON report.

Usage:
    export WP_URL=https://client.com
    export WP_USER=automation-bot
    export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
    python wp_bulk_publish.py articles.csv --report report.json [--status draft] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

ALLOWED_CATEGORIES = {"blog", "guides", "news"}
REQUIRED_FIELDS = [
    "title", "slug", "meta_title", "meta_description",
    "body_html", "primary_keyword", "publish_date", "category",
]
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_TITLE = 60
MAX_META_TITLE = 60
MAX_META_DESC = 160

TRANSIENT_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
BASE_BACKOFF_S = 1.0
REQUEST_TIMEOUT_S = 20
MIN_INTERVAL_S = 1.0  # pacing between write requests

log = logging.getLogger("wp_bulk_publish")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def parse_publish_date(value: str) -> datetime:
    """Parse ISO 8601. Naive datetimes are treated as UTC (documented choice)."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_row(row: dict, now: datetime) -> list[str]:
    """Return a list of human-readable problems; empty list means valid."""
    errors: list[str] = []

    missing = [f for f in REQUIRED_FIELDS if not (row.get(f) or "").strip()]
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")

    title = (row.get("title") or "").strip()
    if len(title) > MAX_TITLE:
        errors.append(f"title is {len(title)} chars (max {MAX_TITLE})")

    meta_title = (row.get("meta_title") or "").strip()
    if len(meta_title) > MAX_META_TITLE:
        errors.append(f"meta_title is {len(meta_title)} chars (max {MAX_META_TITLE})")

    meta_desc = (row.get("meta_description") or "").strip()
    if len(meta_desc) > MAX_META_DESC:
        errors.append(f"meta_description is {len(meta_desc)} chars (max {MAX_META_DESC})")

    slug = (row.get("slug") or "").strip()
    if slug and not SLUG_RE.match(slug):
        errors.append(f"slug '{slug}' has invalid characters (allowed: a-z, 0-9, single hyphens)")

    category = (row.get("category") or "").strip()
    if category and category not in ALLOWED_CATEGORIES:
        errors.append(f"category '{category}' not in allow-list {sorted(ALLOWED_CATEGORIES)}")

    raw_date = (row.get("publish_date") or "").strip()
    if raw_date:
        try:
            if parse_publish_date(raw_date) <= now:
                errors.append(f"publish_date {raw_date} is in the past")
        except ValueError:
            errors.append(f"publish_date '{raw_date}' is not valid ISO 8601")

    return errors


# --------------------------------------------------------------------------- #
# WordPress client
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """Final failure of an API call. `status` is None for timeouts and connection errors."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AlreadyCreated(Exception):
    """Raised from a retry check when the post turns out to exist already.

    A POST can time out on our side and still succeed on the server. Before
    re-sending it, we look the slug up; if the post is there, this exception
    stops the retry loop and carries the existing post back to the caller.
    """

    def __init__(self, post: dict):
        super().__init__(post.get("slug"))
        self.post = post


@dataclass
class WPClient:
    """Thin wrapper around the WordPress REST API (wp/v2) with pacing and retries."""

    base_url: str
    user: str
    app_password: str
    session: requests.Session = field(default_factory=requests.Session)
    _last_write: float = 0.0
    _category_ids: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        self.session.auth = (self.user, self.app_password)
        self.session.headers.update({"User-Agent": "wp-bulk-publish/1.0"})

    def _url(self, path: str) -> str:
        return f"{self.base_url}/wp-json/wp/v2/{path.lstrip('/')}"

    def _pace(self):
        """Keep at least MIN_INTERVAL_S between write requests.

        Only writes are paced: they are the expensive calls on the client's
        server and the ones security plugins/WAFs tend to rate-limit. The GETs
        (slug and category lookups) are cheap and the categories are cached.
        """
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last_write)
        if wait > 0:
            time.sleep(wait)
        self._last_write = time.monotonic()

    def request(self, method: str, path: str, *, before_retry=None, **kwargs) -> requests.Response:
        """HTTP call with exponential backoff on 429/5xx/timeouts.

        - 2xx/3xx: returned right away.
        - 429, 500, 502, 503, 504, timeouts, connection errors: retried up to
          MAX_RETRIES times, waiting 1s, 2s, 4s... (or Retry-After on a 429).
        - Any other 4xx: raised immediately. A 400/401/403/404 won't fix itself,
          so retrying only adds load on the client's site.
        - `before_retry`: optional callback run before each retry. create_post
          uses it to check whether the failed POST actually created the post.

        Raises ApiError when the request finally fails.
        """
        is_write = method.upper() != "GET"
        last_err: ApiError | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            if is_write:
                self._pace()
            resp = None
            try:
                resp = self.session.request(method, self._url(path),
                                            timeout=REQUEST_TIMEOUT_S, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_err = ApiError(None, f"{type(exc).__name__}: {exc}")
            else:
                if resp.status_code < 400:
                    return resp
                message = _error_message(resp)
                if resp.status_code not in TRANSIENT_STATUS:
                    raise ApiError(resp.status_code, message)  # 4xx: don't retry
                last_err = ApiError(resp.status_code, message)

            if attempt == MAX_RETRIES:
                break

            delay = BASE_BACKOFF_S * 2 ** (attempt - 1)
            if resp is not None and resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
            log.warning("transient error on %s %s (attempt %d/%d): %s - retrying in %.1fs",
                        method, path, attempt, MAX_RETRIES, last_err.message, delay)
            time.sleep(delay)

            # Before re-sending a write, give the caller a chance to detect that
            # the previous attempt actually succeeded server-side (timeout case).
            if before_retry is not None:
                before_retry()

        raise last_err  # type: ignore[misc]

    def category_id(self, slug: str) -> int:
        """Resolve a category slug to its ID (the posts endpoint only takes IDs). Cached."""
        if slug not in self._category_ids:
            resp = self.request("GET", "categories", params={"slug": slug})
            data = resp.json()
            if not data:
                raise ApiError(404, f"category '{slug}' does not exist on the site")
            self._category_ids[slug] = data[0]["id"]
        return self._category_ids[slug]

    def find_post_by_slug(self, slug: str) -> dict | None:
        """Return the post with this slug in any status (draft, scheduled, published), or None.

        `status=any` and `context=edit` need an authenticated user with edit
        rights; without them a scheduled or draft post would not be found.
        """
        resp = self.request("GET", "posts",
                            params={"slug": slug, "status": "any", "context": "edit"})
        data = resp.json()
        return data[0] if data else None

    def create_post(self, payload: dict) -> dict:
        """Create the post, without ever creating it twice.

        If the POST fails with a retryable error, the slug is looked up before
        trying again: when the post already exists, the earlier attempt worked
        on the server and we return that post instead of sending a duplicate
        (WordPress would accept it and rename the slug to `<slug>-2`).
        """
        slug = payload["slug"]

        def check_not_created():
            found = self.find_post_by_slug(slug)
            if found is not None:
                raise AlreadyCreated(found)

        try:
            return self.request("POST", "posts", json=payload,
                                before_retry=check_not_created).json()
        except AlreadyCreated as exc:
            log.info("post '%s' was created by an earlier attempt; not re-posting", slug)
            return exc.post


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return body.get("message") or json.dumps(body)[:300]
    except ValueError:
        return resp.text[:300]


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def build_payload(row: dict, category_id: int, status: str) -> dict:
    """Map a CSV row to the /wp/v2/posts request body.

    `date_gmt` is sent in UTC so the schedule doesn't depend on the site's
    timezone setting. The Yoast fields go in `meta`, which only works when the
    keys are registered for REST (see yoast-rest-meta.php).
    """
    publish_utc = parse_publish_date(row["publish_date"])
    return {
        "title": row["title"].strip(),
        "slug": row["slug"].strip(),
        "content": row["body_html"],
        "status": status,  # "future" schedules; "draft" leaves it for review
        "date_gmt": publish_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        "categories": [category_id],
        "meta": {
            "_yoast_wpseo_title": row["meta_title"].strip(),
            "_yoast_wpseo_metadesc": row["meta_description"].strip(),
            "_yoast_wpseo_focuskw": row["primary_keyword"].strip(),
        },
    }


def meta_persisted(post: dict, payload: dict) -> bool:
    """Check that WordPress kept the Yoast meta we sent.

    WordPress silently drops meta keys that aren't registered for REST and
    still answers 201, so a missing or empty key means the SEO fields were
    lost. We don't compare exact values: WordPress sanitizes the text on save
    (sanitize_text_field strips tags, for example), so a valid save can come
    back slightly different from what we sent.
    """
    meta = post.get("meta") or {}
    return all(meta.get(k) for k in payload["meta"])


def run(csv_path: str, client: WPClient | None, status: str, now: datetime) -> list[dict]:
    """Process every CSV row and return one report entry per row.

    Order per row: validate -> skip if the slug already exists on the site ->
    create -> check the Yoast meta. Each row is isolated in its own try/except,
    so an API error or an unexpected exception is recorded in the report and
    the run moves on to the next row. `client=None` means dry run.
    """
    report: list[dict] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing_cols = [c for c in REQUIRED_FIELDS if c not in (reader.fieldnames or [])]
        if missing_cols:
            raise SystemExit(f"CSV is missing columns: {missing_cols}")
        rows = list(reader)

    seen_slugs: set[str] = set()
    for line_no, row in enumerate(rows, start=2):  # line 1 is the header
        entry = {"row": line_no, "slug": (row.get("slug") or "").strip(),
                 "title": (row.get("title") or "").strip()}

        errors = validate_row(row, now)
        if entry["slug"]:  # an empty slug is already reported as a missing field
            if entry["slug"] in seen_slugs:
                errors.append("duplicate slug within this CSV")
            seen_slugs.add(entry["slug"])

        if errors:
            report.append({**entry, "status": "validation_failed", "reasons": errors})
            continue

        if client is None:  # dry run
            report.append({**entry, "status": "valid_dry_run"})
            continue

        try:
            existing = client.find_post_by_slug(entry["slug"])
            if existing:
                report.append({**entry, "status": "skipped_exists",
                               "post_id": existing["id"], "link": existing.get("link")})
                continue

            payload = build_payload(row, client.category_id(row["category"].strip()), status)
            post = client.create_post(payload)

            result = {**entry, "status": "posted", "post_id": post.get("id"),
                      "wp_status": post.get("status"), "link": post.get("link")}
            if not meta_persisted(post, payload):
                result["warning"] = ("Yoast meta not persisted - install yoast-rest-meta.php "
                                     "mu-plugin; post was created without SEO fields")
            report.append(result)
            log.info("row %d posted: id=%s", line_no, post.get("id"))

        except ApiError as exc:
            report.append({**entry, "status": "api_failed",
                           "status_code": exc.status, "message": exc.message})
            log.error("row %d api_failed: %s %s", line_no, exc.status, exc.message)
        except Exception as exc:  # never let one row kill the run
            report.append({**entry, "status": "api_failed",
                           "status_code": None, "message": f"unexpected: {exc!r}"})
            log.exception("row %d unexpected error", line_no)

    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--report", default="report.json")
    ap.add_argument("--status", choices=["future", "draft"], default="future",
                    help="'future' schedules at publish_date; 'draft' leaves posts for review")
    ap.add_argument("--dry-run", action="store_true", help="validate only, no API calls")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = None
    if not args.dry_run:
        try:
            client = WPClient(os.environ["WP_URL"], os.environ["WP_USER"],
                              os.environ["WP_APP_PASSWORD"])
        except KeyError as exc:
            raise SystemExit(f"missing env var {exc}; credentials are never passed as args")
        if not client.base_url.startswith("https://") and "localhost" not in client.base_url \
                and "127.0.0.1" not in client.base_url:
            raise SystemExit("refusing to send Application Password over plain HTTP")

    report = run(args.csv_path, client, args.status, datetime.now(timezone.utc))

    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    counts: dict[str, int] = {}
    for r in report:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log.info("done: %s - report written to %s", counts, args.report)

    # Non-zero exit if anything failed at the API level, so schedulers/CI notice.
    return 1 if counts.get("api_failed") else 0


if __name__ == "__main__":
    sys.exit(main())
