"""Unit tests for wp_bulk_publish.py. Run with: python -m pytest"""

import json
from datetime import datetime, timezone

import pytest
import requests

import wp_bulk_publish as wp

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def valid_row(**overrides) -> dict:
    row = {
        "title": "How to Choose a Standing Desk",
        "slug": "how-to-choose-a-standing-desk",
        "meta_title": "How to Choose a Standing Desk",
        "meta_description": "What actually matters when picking a standing desk.",
        "body_html": "<p>Body</p>",
        "primary_keyword": "standing desk",
        "publish_date": "2026-11-02T09:00:00Z",
        "category": "guides",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_valid_row_has_no_errors():
    assert wp.validate_row(valid_row(), NOW) == []


@pytest.mark.parametrize("overrides, expected", [
    ({"slug": "", "body_html": ""}, "missing required fields: slug, body_html"),
    ({"slug": "Bad_Slug Example!"}, "invalid characters"),
    ({"slug": "double--hyphen"}, "invalid characters"),
    ({"title": "x" * 61}, "title is 61 chars"),
    ({"meta_title": "x" * 61}, "meta_title is 61 chars"),
    ({"meta_description": "x" * 161}, "meta_description is 161 chars"),
    ({"publish_date": "2024-01-15T09:00:00Z"}, "is in the past"),
    ({"publish_date": "next tuesday"}, "not valid ISO 8601"),
    ({"category": "tutorials"}, "not in allow-list"),
])
def test_each_rule_is_flagged(overrides, expected):
    errors = wp.validate_row(valid_row(**overrides), NOW)
    assert any(expected in e for e in errors), errors


def test_limits_are_inclusive():
    row = valid_row(title="x" * 60, meta_title="x" * 60, meta_description="x" * 160)
    assert wp.validate_row(row, NOW) == []


def test_naive_date_is_treated_as_utc():
    assert wp.parse_publish_date("2026-11-02T09:00:00") == datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


def test_offset_date_is_converted_to_utc():
    assert wp.parse_publish_date("2026-11-02T09:00:00-03:00") == datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def test_duplicate_slug_in_csv_is_flagged_but_empty_slugs_are_not(tmp_path):
    rows = [valid_row(), valid_row(), valid_row(slug=""), valid_row(slug="")]
    csv_path = tmp_path / "articles.csv"
    header = list(rows[0])
    lines = [",".join(header)] + [",".join(f'"{r[h]}"' for h in header) for r in rows]
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    report = wp.run(str(csv_path), None, "future", NOW)

    assert report[0]["status"] == "valid_dry_run"
    assert report[1]["reasons"] == ["duplicate slug within this CSV"]
    for empty in report[2:]:
        assert empty["reasons"] == ["missing required fields: slug"]


def test_meta_persisted_detects_dropped_meta_but_tolerates_sanitized_values():
    payload = wp.build_payload(valid_row(), category_id=2, status="future")
    dropped = {"meta": {}}
    sanitized = {"meta": {k: v + " (sanitized)" for k, v in payload["meta"].items()}}
    assert not wp.meta_persisted(dropped, payload)
    assert wp.meta_persisted(sanitized, payload)


# --------------------------------------------------------------------------- #
# HTTP client: retries and duplicate protection
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status: int, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class ScriptedSession(requests.Session):
    """Session that returns (or raises) pre-scripted results in order and records the calls."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url.rsplit("/wp/v2/", 1)[1], kwargs.get("params")))
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(wp, "BASE_BACKOFF_S", 0)
    monkeypatch.setattr(wp, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(wp.time, "sleep", lambda s: None)


def client_with(script) -> wp.WPClient:
    return wp.WPClient("https://example.com", "bot", "secret", session=ScriptedSession(script))


def methods(client):
    return [c[0] for c in client.session.calls]


def test_5xx_is_retried_until_it_succeeds():
    client = client_with([FakeResponse(503), FakeResponse(503), FakeResponse(201, {"id": 7})])
    post = client.request("POST", "posts", json={}).json()
    assert post == {"id": 7}
    assert methods(client) == ["POST", "POST", "POST"]


def test_4xx_is_not_retried():
    client = client_with([FakeResponse(400, {"message": "Invalid parameter(s): content"})])
    with pytest.raises(wp.ApiError) as exc:
        client.request("POST", "posts", json={})
    assert exc.value.status == 400
    assert exc.value.message == "Invalid parameter(s): content"
    assert methods(client) == ["POST"]


def test_gives_up_after_max_retries():
    client = client_with([FakeResponse(500)] * wp.MAX_RETRIES)
    with pytest.raises(wp.ApiError) as exc:
        client.request("GET", "posts")
    assert exc.value.status == 500
    assert len(client.session.calls) == wp.MAX_RETRIES


def test_retry_after_header_is_respected(monkeypatch):
    waits = []
    monkeypatch.setattr(wp.time, "sleep", waits.append)
    client = client_with([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, [])])
    client.request("GET", "posts")
    assert 7 in waits


def test_timeout_that_created_the_post_is_not_posted_again():
    existing = {"id": 42, "slug": "my-post", "meta": {}}
    client = client_with([
        requests.Timeout("read timed out"),  # POST: timed out on our side...
        FakeResponse(200, [existing]),       # ...but the slug lookup finds the post
    ])
    post = client.create_post({"slug": "my-post"})
    assert post == existing
    assert methods(client) == ["POST", "GET"]  # one POST only, no duplicate


def test_timeout_without_a_created_post_is_retried():
    client = client_with([
        requests.Timeout("read timed out"),
        FakeResponse(200, []),               # slug lookup: not there
        FakeResponse(201, {"id": 43, "slug": "my-post"}),
    ])
    post = client.create_post({"slug": "my-post"})
    assert post["id"] == 43
    assert methods(client) == ["POST", "GET", "POST"]


def test_api_error_on_one_row_does_not_stop_the_run(tmp_path, monkeypatch):
    header = list(valid_row())
    rows = [valid_row(slug="first"), valid_row(slug="second")]
    csv_path = tmp_path / "articles.csv"
    csv_path.write_text("\n".join([",".join(header)] +
                                  [",".join(f'"{r[h]}"' for h in header) for r in rows]),
                        encoding="utf-8")
    meta = {"_yoast_wpseo_title": "t", "_yoast_wpseo_metadesc": "d", "_yoast_wpseo_focuskw": "k"}
    client = client_with([
        FakeResponse(200, []),                             # row 1: slug lookup
        FakeResponse(200, [{"id": 2, "slug": "guides"}]),  # row 1: category lookup
        FakeResponse(400, {"message": "bad content"}),     # row 1: POST rejected
        FakeResponse(200, []),                             # row 2: slug lookup (category cached)
        FakeResponse(201, {"id": 9, "status": "future", "meta": meta}),
    ])

    report = wp.run(str(csv_path), client, "future", NOW)

    assert [r["status"] for r in report] == ["api_failed", "posted"]
    assert report[0]["status_code"] == 400
    assert "warning" not in report[1]
