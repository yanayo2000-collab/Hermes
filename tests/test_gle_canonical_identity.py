from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from app.tugao_bi import (
    GLE_CANONICAL_IDENTITY_CONTRACT_VERSION,
    TugaoBiSafetyError,
    ensure_tugao_bind_tables,
    normalize_tugao_bind_event,
    sync_tugao_bind_success_events,
    upsert_tugao_bind_event,
    verify_tugao_canonical_identity,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_tugao_bind_tables(conn)
    conn.executescript(
        """
        CREATE TABLE leads (
            lead_id TEXT PRIMARY KEY,
            matched_customer_id TEXT NOT NULL DEFAULT '',
            crm_verified_at TEXT,
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE customer_projection (
            customer_id TEXT PRIMARY KEY,
            lead_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    return conn


def _close_pair(
    conn: sqlite3.Connection,
    lead_id: str = "lead-alpha",
    customer_id: str = "customer-alpha",
) -> None:
    conn.execute(
        "INSERT INTO leads (lead_id,matched_customer_id) VALUES (?,?)",
        (lead_id, customer_id),
    )
    conn.execute(
        "INSERT INTO customer_projection (customer_id,lead_id) VALUES (?,?)",
        (customer_id, lead_id),
    )


def _raw(
    *,
    event_id: str = "event-alpha",
    version: object = GLE_CANONICAL_IDENTITY_CONTRACT_VERSION,
    lead_id: object = "lead-alpha",
    customer_id: object = "customer-alpha",
    customer_user_id: str = "user-explicit",
) -> dict:
    result = {
        "event_id": event_id,
        "bind_status": "success",
        "country": "MX",
        "campaign_id": "campaign-alpha",
        "adset_id": "adset-alpha",
        "ad_id": "ad-alpha",
        "customer_user_id": customer_user_id,
    }
    if version is not ...:
        result["identity_contract_version"] = version
    if lead_id is not ...:
        result["lead_id"] = lead_id
    if customer_id is not ...:
        result["customer_id"] = customer_id
    return result


def _persist(conn: sqlite3.Connection, raw: dict) -> sqlite3.Row:
    normalized = normalize_tugao_bind_event(raw)
    upsert_tugao_bind_event(conn, normalized)
    return conn.execute(
        "SELECT * FROM tugao_bind_success_raw_events WHERE event_id=?",
        (raw["event_id"],),
    ).fetchone()


def test_schema_upgrade_preserves_legacy_row_bytes_hash_and_count() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tugao_bind_success_raw_events (
            event_id TEXT PRIMARY KEY,
            bind_status TEXT NOT NULL,
            occurred_at_utc TEXT,
            updated_at_utc TEXT,
            business_date TEXT,
            project TEXT,
            country TEXT,
            media_source TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            adset_id TEXT,
            adset_name TEXT,
            ad_id TEXT,
            ad_name TEXT,
            bind_id TEXT,
            customer_user_id TEXT,
            user_key TEXT,
            has_wa INTEGER NOT NULL DEFAULT 0,
            raw_payload_sha256 TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL,
            first_seen_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL
        );
        """
    )
    payload = '{"customer_id":"legacy-user","event_id":"legacy-event"}'
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO tugao_bind_success_raw_events
           (event_id,bind_status,customer_user_id,user_key,raw_payload_sha256,
            raw_payload_json,first_seen_at_utc,last_seen_at_utc)
           VALUES ('legacy-event','success','legacy-user','legacy-user',?,?,?,?)""",
        (payload_hash, payload, "first", "last"),
    )
    before = tuple(conn.execute(
        """SELECT event_id,bind_status,customer_user_id,user_key,
                  raw_payload_sha256,raw_payload_json,first_seen_at_utc,last_seen_at_utc
           FROM tugao_bind_success_raw_events"""
    ).fetchone())

    ensure_tugao_bind_tables(conn)
    ensure_tugao_bind_tables(conn)

    after = tuple(conn.execute(
        """SELECT event_id,bind_status,customer_user_id,user_key,
                  raw_payload_sha256,raw_payload_json,first_seen_at_utc,last_seen_at_utc
           FROM tugao_bind_success_raw_events"""
    ).fetchone())
    identity = conn.execute(
        """SELECT identity_contract_version,canonical_lead_id,canonical_customer_id,
                  canonical_identity_status,canonical_identity_reason
           FROM tugao_bind_success_raw_events"""
    ).fetchone()
    assert before == after
    assert conn.execute("SELECT COUNT(*) FROM tugao_bind_success_raw_events").fetchone()[0] == 1
    assert tuple(identity) == (None, None, None, "LEGACY_UNVERIFIED", "LEGACY_UNVERIFIED")


def test_legacy_customer_id_behavior_is_preserved_but_never_canonical() -> None:
    conn = _connection()
    raw = _raw(version=..., lead_id=..., customer_id="legacy-user", customer_user_id="")
    row = _persist(conn, raw)
    assert row["customer_user_id"] == "legacy-user"
    assert row["canonical_lead_id"] is None
    assert row["canonical_customer_id"] is None
    assert row["canonical_identity_status"] == "LEGACY_UNVERIFIED"


def test_v1_customer_id_is_canonical_and_never_customer_user_id_fallback() -> None:
    conn = _connection()
    _close_pair(conn)
    row = _persist(conn, _raw(customer_user_id=""))
    assert row["customer_user_id"] == ""
    assert row["canonical_customer_id"] == "customer-alpha"
    assert row["canonical_identity_status"] == "VERIFIED"


@pytest.mark.parametrize(
    ("lead_id", "customer_id", "reason"),
    [
        (..., "customer-alpha", "CANONICAL_IDENTITY_MISSING"),
        ("lead-alpha", ..., "CANONICAL_IDENTITY_MISSING"),
        ("", "customer-alpha", "CANONICAL_IDENTITY_INVALID"),
        (123, "customer-alpha", "CANONICAL_IDENTITY_INVALID"),
        (" lead-alpha", "customer-alpha", "CANONICAL_IDENTITY_INVALID"),
        ("lead-alpha", "customer-alpha ", "CANONICAL_IDENTITY_INVALID"),
    ],
)
def test_v1_requires_two_exact_unmodified_ids(
    lead_id: object, customer_id: object, reason: str
) -> None:
    conn = _connection()
    row = _persist(conn, _raw(lead_id=lead_id, customer_id=customer_id))
    assert row["canonical_lead_id"] is None
    assert row["canonical_customer_id"] is None
    assert row["canonical_identity_status"] == "BLOCKED"
    assert row["canonical_identity_reason"] == reason


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        (
            {"identity_contract_version": " gle-canonical-identity-v1"},
            "IDENTITY_CONTRACT_VERSION_INVALID",
        ),
        (
            {"identity_contract_version": "gle-canonical-identity-v2"},
            "IDENTITY_CONTRACT_VERSION_UNSUPPORTED",
        ),
        ({"lead_id": ...}, "CANONICAL_IDENTITY_MISSING"),
        ({"lead_id": " lead-alpha"}, "CANONICAL_IDENTITY_INVALID"),
    ],
)
def test_first_contract_anomaly_is_sticky_before_any_pair_is_accepted(
    first: dict, reason: str
) -> None:
    conn = _connection()
    _close_pair(conn)
    first_raw = _raw()
    for key, value in first.items():
        if value is ...:
            first_raw.pop(key)
        else:
            first_raw[key] = value
    blocked = _persist(conn, first_raw)
    replay = _persist(conn, _raw())
    assert blocked["canonical_identity_reason"] == reason
    assert replay["canonical_lead_id"] is None
    assert replay["canonical_customer_id"] is None
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == reason


def test_unknown_or_malformed_version_is_not_legacy_and_not_canonical() -> None:
    conn = _connection()
    malformed = _persist(conn, _raw(event_id="bad-version", version=" gle-canonical-identity-v1"))
    unknown = _persist(conn, _raw(event_id="unknown-version", version="gle-canonical-identity-v2"))
    assert malformed["customer_user_id"] == "user-explicit"
    assert malformed["canonical_identity_reason"] == "IDENTITY_CONTRACT_VERSION_INVALID"
    assert unknown["customer_user_id"] == "user-explicit"
    assert unknown["canonical_identity_reason"] == "IDENTITY_CONTRACT_VERSION_UNSUPPORTED"


def test_nested_identity_is_never_a_canonical_fallback() -> None:
    conn = _connection()
    raw = _raw(lead_id=..., customer_id=...)
    raw["identity"] = {"lead_id": "lead-alpha", "customer_id": "customer-alpha"}
    row = _persist(conn, raw)
    assert row["canonical_identity_reason"] == "CANONICAL_IDENTITY_MISSING"


def test_same_pair_is_idempotent_and_preserves_first_contract() -> None:
    conn = _connection()
    _close_pair(conn)
    first = _persist(conn, _raw())
    second = _persist(conn, _raw(customer_user_id="new-explicit-user"))
    assert first["identity_contract_version"] == GLE_CANONICAL_IDENTITY_CONTRACT_VERSION
    assert second["identity_contract_version"] == GLE_CANONICAL_IDENTITY_CONTRACT_VERSION
    assert second["canonical_lead_id"] == "lead-alpha"
    assert second["canonical_customer_id"] == "customer-alpha"
    assert second["canonical_identity_status"] == "VERIFIED"


def test_missing_after_valid_is_permanent_and_preserves_pair() -> None:
    conn = _connection()
    _close_pair(conn)
    _persist(conn, _raw())
    missing = _persist(conn, _raw(version=..., lead_id=..., customer_id=...))
    replay = _persist(conn, _raw())
    assert missing["canonical_lead_id"] == "lead-alpha"
    assert missing["canonical_customer_id"] == "customer-alpha"
    assert missing["canonical_identity_reason"] == "EVENT_IDENTITY_MISSING_AFTER_VALID"
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == "EVENT_IDENTITY_MISSING_AFTER_VALID"


def test_different_pair_is_permanent_drift_and_never_overwrites_first_pair() -> None:
    conn = _connection()
    _close_pair(conn)
    _persist(conn, _raw())
    drift = _persist(
        conn,
        _raw(lead_id="lead-other", customer_id="customer-other"),
    )
    replay = _persist(conn, _raw())
    assert drift["canonical_lead_id"] == "lead-alpha"
    assert drift["canonical_customer_id"] == "customer-alpha"
    assert drift["canonical_identity_reason"] == "EVENT_IDENTITY_DRIFT"
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == "EVENT_IDENTITY_DRIFT"


def test_partial_stored_canonical_evidence_is_blocked_and_never_overwritten() -> None:
    conn = _connection()
    legacy = _raw(version=..., lead_id=..., customer_id="legacy-user")
    _persist(conn, legacy)
    conn.execute(
        """UPDATE tugao_bind_success_raw_events
           SET canonical_lead_id='stored-lead'
           WHERE event_id='event-alpha'"""
    )
    row = _persist(conn, _raw())
    assert row["identity_contract_version"] is None
    assert row["canonical_lead_id"] == "stored-lead"
    assert row["canonical_customer_id"] is None
    assert row["canonical_identity_reason"] == "CANONICAL_IDENTITY_STORED_INCOMPLETE"


def test_latest_raw_payload_can_change_without_changing_canonical_evidence() -> None:
    conn = _connection()
    _close_pair(conn)
    first = _persist(conn, _raw())
    drift_raw = _raw(lead_id="lead-other", customer_id="customer-other")
    drift_raw["ad_name"] = "latest"
    second = _persist(conn, drift_raw)
    assert second["raw_payload_sha256"] != first["raw_payload_sha256"]
    assert json.loads(second["raw_payload_json"])["ad_name"] == "latest"
    assert second["canonical_lead_id"] == "lead-alpha"
    assert second["canonical_customer_id"] == "customer-alpha"


def test_missing_crm_counterpart_can_be_verified_later_with_same_pair() -> None:
    conn = _connection()
    pending = _persist(conn, _raw())
    assert pending["canonical_identity_status"] == "PENDING_VERIFICATION"
    assert pending["canonical_identity_reason"] == "CANONICAL_IDENTITY_NOT_IN_CRM"
    _close_pair(conn)
    verified = _persist(conn, _raw())
    assert verified["canonical_identity_status"] == "VERIFIED"
    assert verified["canonical_identity_reason"] == ""


def test_existing_conflict_is_not_misclassified_as_missing_counterpart() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-alpha','customer-other')"
    )
    row = _persist(conn, _raw())
    assert row["canonical_identity_status"] == "BLOCKED"
    assert row["canonical_identity_reason"] == "LEAD_CUSTOMER_LINK_CONFLICT"


def test_missing_direct_customer_detects_reverse_customer_conflict() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-alpha','customer-alpha')"
    )
    conn.execute(
        "INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-other','lead-alpha')"
    )
    row = _persist(conn, _raw())
    assert row["canonical_identity_status"] == "BLOCKED"
    assert row["canonical_identity_reason"] == "LEAD_CUSTOMER_LINK_CONFLICT"


def test_missing_direct_lead_detects_reverse_lead_conflict() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-alpha','lead-alpha')"
    )
    conn.execute(
        "INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-other','customer-alpha')"
    )
    row = _persist(conn, _raw())
    assert row["canonical_identity_status"] == "BLOCKED"
    assert row["canonical_identity_reason"] == "LEAD_CUSTOMER_LINK_CONFLICT"


def test_crm_conflict_is_sticky_even_if_crm_is_later_corrected() -> None:
    conn = _connection()
    conn.executescript(
        """INSERT INTO leads (lead_id,matched_customer_id)
           VALUES ('lead-alpha','customer-other');
           INSERT INTO customer_projection (customer_id,lead_id)
           VALUES ('customer-alpha','lead-alpha');"""
    )
    blocked = _persist(conn, _raw())
    conn.execute(
        "UPDATE leads SET matched_customer_id='customer-alpha' WHERE lead_id='lead-alpha'"
    )
    replay = _persist(conn, _raw())
    assert blocked["canonical_identity_reason"] == "LEAD_CUSTOMER_LINK_CONFLICT"
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == "LEAD_CUSTOMER_LINK_CONFLICT"


@pytest.mark.parametrize(
    ("setup_sql", "reason"),
    [
        (
            """INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-alpha','customer-other');
               INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-alpha','lead-alpha');""",
            "LEAD_CUSTOMER_LINK_CONFLICT",
        ),
        (
            """INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-alpha','customer-alpha');
               INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-other','customer-alpha');
               INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-alpha','lead-alpha');""",
            "AMBIGUOUS_CANONICAL_IDENTITY",
        ),
        (
            """INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-alpha','customer-alpha');
               INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-alpha','lead-alpha');
               INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-other','lead-alpha');""",
            "AMBIGUOUS_CANONICAL_IDENTITY",
        ),
    ],
)
def test_verifier_requires_exact_unique_bidirectional_closure(
    setup_sql: str, reason: str
) -> None:
    conn = _connection()
    conn.executescript(setup_sql)
    row = _persist(conn, _raw())
    assert row["canonical_identity_status"] == "BLOCKED"
    assert row["canonical_identity_reason"] == reason


def test_crm_whitespace_is_invalid_and_is_not_trimmed() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO leads (lead_id,matched_customer_id) VALUES ('lead-alpha',' customer-alpha')"
    )
    conn.execute(
        "INSERT INTO customer_projection (customer_id,lead_id) VALUES ('customer-alpha','lead-alpha')"
    )
    row = _persist(conn, _raw())
    assert row["canonical_identity_status"] == "BLOCKED"
    assert row["canonical_identity_reason"] == "CANONICAL_IDENTITY_INVALID"


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        ("identity_contract_version", "bad-v"),
        ("canonical_lead_id", " lead-alpha"),
        ("canonical_customer_id", "customer-alpha "),
    ],
)
def test_corrupt_stored_contract_is_sticky_and_never_repaired(
    column: str, corrupt_value: str
) -> None:
    conn = _connection()
    _close_pair(conn)
    _persist(conn, _raw())
    conn.execute(
        f"UPDATE tugao_bind_success_raw_events SET {column}=? WHERE event_id='event-alpha'",
        (corrupt_value,),
    )
    replay = _persist(conn, _raw())
    assert replay[column] == corrupt_value
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == "CANONICAL_IDENTITY_STORED_INCOMPLETE"


@pytest.mark.parametrize(
    ("stored_status", "stored_reason"),
    [
        ("VERIFIED", "CANONICAL_IDENTITY_NOT_IN_CRM"),
        ("PENDING_VERIFICATION", ""),
        ("PENDING_VERIFICATION", "LEAD_CUSTOMER_LINK_CONFLICT"),
        ("BLOCKED", ""),
        ("LEGACY_UNVERIFIED", "LEGACY_UNVERIFIED"),
    ],
)
def test_illegal_stored_status_reason_combinations_are_permanently_blocked(
    stored_status: str, stored_reason: str
) -> None:
    conn = _connection()
    _close_pair(conn)
    _persist(conn, _raw())
    conn.execute(
        """UPDATE tugao_bind_success_raw_events
           SET canonical_identity_status=?,canonical_identity_reason=?
           WHERE event_id='event-alpha'""",
        (stored_status, stored_reason),
    )
    replay = _persist(conn, _raw())
    assert replay["canonical_lead_id"] == "lead-alpha"
    assert replay["canonical_customer_id"] == "customer-alpha"
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == "CANONICAL_IDENTITY_STORED_INCOMPLETE"


@pytest.mark.parametrize(
    ("stored_status", "stored_reason"),
    [
        ("VERIFIED", ""),
        ("PENDING_VERIFICATION", "CANONICAL_IDENTITY_NOT_IN_CRM"),
    ],
)
def test_illegal_pairless_status_combinations_are_stored_incomplete(
    stored_status: str, stored_reason: str
) -> None:
    conn = _connection()
    _persist(conn, _raw(version=..., lead_id=..., customer_id="legacy-user"))
    conn.execute(
        """UPDATE tugao_bind_success_raw_events
           SET canonical_identity_status=?,canonical_identity_reason=?
           WHERE event_id='event-alpha'""",
        (stored_status, stored_reason),
    )
    replay = _persist(conn, _raw())
    assert replay["canonical_lead_id"] is None
    assert replay["canonical_customer_id"] is None
    assert replay["canonical_identity_status"] == "BLOCKED"
    assert replay["canonical_identity_reason"] == "CANONICAL_IDENTITY_STORED_INCOMPLETE"


def test_noncanonical_fields_never_verify_identity() -> None:
    conn = _connection()
    _close_pair(conn)
    raw = _raw(lead_id=..., customer_id=...)
    raw.update(
        {
            "phone": "redacted-in-fixture",
            "name": "redacted-name",
            "bind_id": "lead-alpha",
            "customer_user_id": "customer-alpha",
        }
    )
    with pytest.raises(TugaoBiSafetyError) as exc_info:
        normalize_tugao_bind_event(raw)
    assert "redacted-in-fixture" not in str(exc_info.value)
    assert "redacted-name" not in str(exc_info.value)


def test_contract_failures_do_not_expose_full_ids() -> None:
    conn = _connection()
    secret_lead = "lead-do-not-print-123456"
    secret_customer = "customer-do-not-print-654321"
    row = _persist(
        conn,
        _raw(lead_id=f" {secret_lead}", customer_id=secret_customer),
    )
    rendered = f"{row['canonical_identity_status']}:{row['canonical_identity_reason']}"
    assert secret_lead not in rendered
    assert secret_customer not in rendered


def test_verifier_queries_are_exact_and_result_bounded() -> None:
    statements = []
    conn = _connection()
    _close_pair(conn)
    conn.set_trace_callback(
        lambda statement: statements.append(statement.split(" WHERE ", 1)[0])
    )
    _persist(conn, _raw())
    verifier_selects = [
        statement
        for statement in statements
        if statement.startswith("SELECT lead_id,matched_customer_id FROM leads")
        or statement.startswith("SELECT customer_id,lead_id FROM customer_projection")
        or statement.startswith("SELECT lead_id FROM leads")
        or statement.startswith("SELECT customer_id FROM customer_projection")
    ]
    assert len(verifier_selects) == 4
    source = inspect.getsource(verify_tugao_canonical_identity)
    assert "WHERE lead_id=? LIMIT 2" in source
    assert "WHERE customer_id=? LIMIT 2" in source
    assert "WHERE matched_customer_id=? ORDER BY lead_id LIMIT 2" in source
    assert "WHERE lead_id=? ORDER BY customer_id LIMIT 2" in source


def test_existing_tugao_sync_path_persists_verified_contract() -> None:
    class Client:
        def iter_bind_success_events(self, **_: object) -> tuple[list[dict], dict]:
            return [_raw()], {"pages": 1, "next_cursor": "", "truncated": False}

    conn = _connection()
    _close_pair(conn)
    result = sync_tugao_bind_success_events(
        conn,
        Client(),
        start_time="2026-08-01T00:00:00Z",
        end_time="2026-08-02T00:00:00Z",
    )
    row = conn.execute(
        "SELECT canonical_identity_status FROM tugao_bind_success_raw_events"
    ).fetchone()
    assert result["status"] == "success"
    assert result["rows_inserted"] == 1
    assert row[0] == "VERIFIED"


def test_concurrent_first_pair_cannot_overwrite_the_winning_pair(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite3"
    setup = sqlite3.connect(db_path)
    setup.row_factory = sqlite3.Row
    ensure_tugao_bind_tables(setup)
    setup.executescript(
        """CREATE TABLE leads (
               lead_id TEXT PRIMARY KEY,
               matched_customer_id TEXT NOT NULL DEFAULT ''
           );
           CREATE TABLE customer_projection (
               customer_id TEXT PRIMARY KEY,
               lead_id TEXT NOT NULL DEFAULT ''
           );
           INSERT INTO leads VALUES ('lead-first','customer-first');
           INSERT INTO customer_projection VALUES ('customer-first','lead-first');
           INSERT INTO leads VALUES ('lead-second','customer-second');
           INSERT INTO customer_projection VALUES ('customer-second','lead-second');"""
    )
    setup.commit()
    setup.close()

    winner = sqlite3.connect(db_path)
    winner.row_factory = sqlite3.Row
    contender = sqlite3.connect(db_path)
    contender.row_factory = sqlite3.Row
    winner_payload = normalize_tugao_bind_event(
        _raw(lead_id="lead-first", customer_id="customer-first")
    )
    contender_payload = normalize_tugao_bind_event(
        _raw(lead_id="lead-second", customer_id="customer-second")
    )

    class InterleavingConnection:
        def __init__(self) -> None:
            self.triggered = False

        def execute(self, sql: str, params: tuple = ()):
            cursor = contender.execute(sql, params)
            if not self.triggered and "SELECT raw_payload_sha256" in sql:
                self.triggered = True
                upsert_tugao_bind_event(winner, winner_payload)
                winner.commit()
            return cursor

    upsert_tugao_bind_event(InterleavingConnection(), contender_payload)
    contender.commit()
    row = contender.execute(
        """SELECT canonical_lead_id,canonical_customer_id,
                  canonical_identity_status,canonical_identity_reason
           FROM tugao_bind_success_raw_events WHERE event_id='event-alpha'"""
    ).fetchone()
    winner.close()
    contender.close()
    assert tuple(row) == (
        "lead-first",
        "customer-first",
        "BLOCKED",
        "EVENT_IDENTITY_DRIFT",
    )
