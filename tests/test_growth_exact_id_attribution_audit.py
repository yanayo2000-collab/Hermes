from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.growth import exact_id_attribution_audit as audit_module
from app.growth.exact_id_attribution_audit import (
    ATTRIBUTION_VERSION,
    DEDUPE_VERSION,
    QUALIFICATION_RULE_VERSION,
    AuditInput,
    SourceAuditError,
    audit_snapshot,
    canonical_json,
    exit_code_for_report,
    open_readonly_snapshot,
)
from scripts.audit_gle_exact_id_attribution import main as cli_main


WINDOW_START = "2026-08-01T00:00:00Z"
WINDOW_END = "2026-08-02T00:00:00Z"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_snapshot(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE ad_dashboard_fact_rows (
                row_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                account_id TEXT NOT NULL,
                country TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                adset_id TEXT NOT NULL,
                ad_id TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE ad_experiment (
                experiment_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                country TEXT NOT NULL,
                source_campaign_id TEXT NOT NULL,
                source_adset_id TEXT NOT NULL,
                source_ad_id TEXT NOT NULL,
                control_definition_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE tugao_bind_success_raw_events (
                event_id TEXT PRIMARY KEY,
                bind_status TEXT NOT NULL,
                occurred_at_utc TEXT,
                updated_at_utc TEXT,
                business_date TEXT,
                project TEXT,
                country TEXT,
                campaign_id TEXT,
                adset_id TEXT,
                ad_id TEXT,
                bind_id TEXT,
                customer_user_id TEXT,
                user_key TEXT,
                raw_payload_sha256 TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL
            );
            CREATE TABLE leads (
                lead_id TEXT PRIMARY KEY,
                matched_customer_id TEXT NOT NULL DEFAULT '',
                crm_verified_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE customer_projection (
                customer_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )
        _insert_experiment(conn)
        conn.execute(
            """INSERT INTO ad_dashboard_fact_rows
               (row_id,date,account_id,country,campaign_id,adset_id,ad_id,payload_json)
               VALUES ('fact-1','2026-08-01','act-1','MX','campaign-1','adset-1','ad-1','{}')"""
        )
        conn.execute(
            """INSERT INTO leads
               (lead_id,matched_customer_id,crm_verified_at,updated_at)
               VALUES ('lead-1','customer-1','2026-08-01T03:00:00Z','2026-08-01T03:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO customer_projection
               (customer_id,lead_id,updated_at)
               VALUES ('customer-1','lead-1','2026-08-01T03:00:00Z')"""
        )
        _insert_bind(
            conn,
            event_id="event-1",
            raw={"lead_id": "lead-1", "customer_id": "customer-1"},
        )


def _insert_experiment(
    conn: sqlite3.Connection,
    *,
    experiment_id: str = "experiment-1",
    ad_id: str = "ad-1",
    study_id: str = "study-1",
    study_cell_id: str | None = None,
) -> None:
    control = {
        "meta_randomization": {
            "study_id": study_id,
            "study_cell_id": study_cell_id or f"cell-{experiment_id}",
            "readback_verified": True,
        }
    }
    conn.execute(
        """INSERT INTO ad_experiment
           (experiment_id,account_id,country,source_campaign_id,source_adset_id,
            source_ad_id,control_definition_json)
           VALUES (?,?,?,?,?,?,?)""",
        (
            experiment_id,
            "act-1",
            "MX",
            "campaign-1",
            "adset-1",
            ad_id,
            json.dumps(control, sort_keys=True),
        ),
    )


def _insert_bind(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    raw: dict,
    occurred_at: str = "2026-08-01T02:00:00Z",
    ad_id: str = "ad-1",
) -> None:
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO tugao_bind_success_raw_events
           (event_id,bind_status,occurred_at_utc,updated_at_utc,business_date,
            project,country,campaign_id,adset_id,ad_id,bind_id,customer_user_id,
            user_key,raw_payload_sha256,raw_payload_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            "success",
            occurred_at,
            occurred_at,
            "2026-08-01",
            "TUGAO",
            "MX",
            "campaign-1",
            "adset-1",
            ad_id,
            raw.get("bind_id", ""),
            raw.get("customer_user_id", ""),
            raw.get("user_key", ""),
            hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
            raw_json,
        ),
    )


def _input(path: Path, *, max_events: int = 10000, experiments=("experiment-1",)) -> AuditInput:
    return AuditInput(
        db_path=path,
        expected_db_sha256=_sha256(path),
        account_id="act-1",
        market="MX",
        experiment_ids=tuple(experiments),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        project="TUGAO",
        max_events=max_events,
    )


def test_exact_identity_chain_is_measured_but_unfrozen_qualification_blocks(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)

    report = audit_snapshot(_input(db_path))

    assert report["status"] == "BLOCKED"
    assert report["blocking_reasons"] == [
        "QUALIFICATION_RULE_UNFROZEN",
        "READBACK_PROVENANCE_UNAUDITED",
    ]
    assert report["versions"] == {
        "attribution": ATTRIBUTION_VERSION,
        "dedupe": DEDUPE_VERSION,
        "qualification_rule": QUALIFICATION_RULE_VERSION,
    }
    assert report["counts"]["candidate_event_count"] == 1
    assert report["counts"]["exact_meta_event_count"] == 1
    assert report["counts"]["exact_identity_event_count"] == 1
    assert report["coverage"]["exact_meta"] == 1.0
    assert report["coverage"]["exact_identity"] == 1.0
    assert report["crm_verification_latency_seconds"] == {
        "count": 1,
        "p50": 3600.0,
        "p90": 3600.0,
        "p95": 3600.0,
        "max": 3600.0,
    }
    assert exit_code_for_report(report) == 2
    assert len(report["source_schema_hash"]) == 64
    assert len(report["row_evidence_hash"]) == 64
    assert len(report["report_hash"]) == 64


@pytest.mark.parametrize(
    "canonical_raw",
    [{"lead_id": "lead-1"}, {"customer_id": "customer-1"}],
)
def test_each_supported_canonical_namespace_is_accepted_exactly(
    tmp_path: Path, canonical_raw: dict
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        _insert_bind(conn, event_id="canonical-event", raw=canonical_raw)

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 1
    assert report["missing_reason_counts"] == {}
    assert report["ambiguous_reason_counts"] == {}


def test_lead_only_requires_customer_projection_pair(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        conn.execute("DELETE FROM customer_projection")
        _insert_bind(conn, event_id="lead-only", raw={"lead_id": "lead-1"})

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["missing_reason_counts"] == {
        "CANONICAL_IDENTITY_NOT_IN_CRM": 1
    }


def test_customer_only_rejects_lead_customer_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        conn.execute(
            "UPDATE leads SET matched_customer_id='different-customer' WHERE lead_id='lead-1'"
        )
        _insert_bind(
            conn, event_id="customer-only", raw={"customer_id": "customer-1"}
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["ambiguous_reason_counts"] == {
        "LEAD_CUSTOMER_LINK_CONFLICT": 1
    }


def test_customer_only_requires_lead_pair(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        conn.execute("DELETE FROM leads")
        _insert_bind(
            conn, event_id="customer-only", raw={"customer_id": "customer-1"}
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["missing_reason_counts"] == {
        "CANONICAL_IDENTITY_NOT_IN_CRM": 1
    }


@pytest.mark.parametrize("duplicate_side", ["lead", "customer"])
def test_reverse_identity_duplicates_are_ambiguous(
    tmp_path: Path, duplicate_side: str
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        if duplicate_side == "lead":
            conn.execute(
                "INSERT INTO customer_projection (customer_id,lead_id,updated_at) "
                "VALUES ('customer-2','lead-1','2026-08-01T03:00:00Z')"
            )
            raw = {"lead_id": "lead-1"}
        else:
            conn.execute(
                "INSERT INTO leads (lead_id,matched_customer_id,crm_verified_at,updated_at) "
                "VALUES ('lead-2','customer-1','2026-08-01T03:00:00Z','2026-08-01T03:00:00Z')"
            )
            raw = {"customer_id": "customer-1"}
        _insert_bind(conn, event_id="reverse-duplicate", raw=raw)

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["ambiguous_reason_counts"] == {
        "AMBIGUOUS_CANONICAL_IDENTITY": 1
    }


def test_lead_and_customer_namespaces_dedupe_to_same_customer(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        _insert_bind(conn, event_id="lead-event", raw={"lead_id": "lead-1"})
        _insert_bind(
            conn, event_id="customer-event", raw={"customer_id": "customer-1"}
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 2
    assert report["counts"]["deduped_canonical_identity_count"] == 1


def test_identity_scope_above_1000_keys_does_not_use_host_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    leads = []
    projections = []
    events = []
    for index in range(1001):
        lead_id = f"lead-{index}"
        customer_id = f"customer-{index}"
        raw_json = canonical_json(
            {"lead_id": lead_id, "customer_id": customer_id}
        )
        leads.append(
            (
                lead_id,
                customer_id,
                "2026-08-01T03:00:00Z",
                "2026-08-01T03:00:00Z",
            )
        )
        projections.append(
            (customer_id, lead_id, "2026-08-01T03:00:00Z")
        )
        events.append(
            (
                f"event-{index}",
                "success",
                "2026-08-01T02:00:00Z",
                "2026-08-01T02:00:00Z",
                "2026-08-01",
                "TUGAO",
                "MX",
                "campaign-1",
                "adset-1",
                "ad-1",
                "",
                "",
                "",
                hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                raw_json,
            )
        )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM customer_projection")
        conn.executemany(
            "INSERT INTO leads "
            "(lead_id,matched_customer_id,crm_verified_at,updated_at) "
            "VALUES (?,?,?,?)",
            leads,
        )
        conn.executemany(
            "INSERT INTO customer_projection (customer_id,lead_id,updated_at) "
            "VALUES (?,?,?)",
            projections,
        )
        conn.executemany(
            "INSERT INTO tugao_bind_success_raw_events "
            "(event_id,bind_status,occurred_at_utc,updated_at_utc,business_date,"
            "project,country,campaign_id,adset_id,ad_id,bind_id,customer_user_id,"
            "user_key,raw_payload_sha256,raw_payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            events,
        )

    original_open = audit_module.open_readonly_snapshot

    class LowVariableLimitConnection:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, parameters=()):
            if len(parameters) > 128:
                raise sqlite3.OperationalError("test host-variable limit exceeded")
            return self._conn.execute(sql, parameters)

    @contextmanager
    def open_with_low_variable_limit(path: Path):
        with original_open(path) as conn:
            yield LowVariableLimitConnection(conn)

    monkeypatch.setattr(
        audit_module, "open_readonly_snapshot", open_with_low_variable_limit
    )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["candidate_event_count"] == 1001
    assert report["counts"]["exact_identity_event_count"] == 1001
    assert report["counts"]["deduped_canonical_identity_count"] == 1001


@pytest.mark.parametrize(
    "fallback_raw",
    [
        {"customer_user_id": "lead-1"},
        {"user_key": "lead-1"},
        {"bind_id": "lead-1"},
        {"event_id": "lead-1"},
        {"phone": "+52-555-000-1111"},
        {"name": "Sensitive Person"},
    ],
)
def test_every_noncanonical_fallback_is_rejected(
    tmp_path: Path, fallback_raw: dict
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        _insert_bind(conn, event_id="fallback-event", raw=fallback_raw)

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_meta_event_count"] == 1
    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["missing_reason_counts"] == {"MISSING_CANONICAL_IDENTITY": 1}
    rendered = canonical_json(report)
    for raw_value in fallback_raw.values():
        assert str(raw_value) not in rendered


def test_empty_object_is_missing_identity_and_wrong_type_is_invalid(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        _insert_bind(conn, event_id="missing", raw={})
        _insert_bind(conn, event_id="invalid", raw={"lead_id": 123})

    report = audit_snapshot(_input(db_path))

    assert report["reason_counts"] == {
        "CANONICAL_IDENTITY_INVALID": 1,
        "MISSING_CANONICAL_IDENTITY": 1,
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"lead_id": " lead-1 ", "customer_id": "customer-1"},
        {"lead_id": "lead-1", "customer_id": " customer-1 "},
    ],
)
def test_payload_canonical_ids_with_whitespace_are_invalid(
    tmp_path: Path, raw: dict
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM tugao_bind_success_raw_events")
        _insert_bind(conn, event_id="whitespace-id", raw=raw)

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["reason_counts"] == {"CANONICAL_IDENTITY_INVALID": 1}


def test_crm_identity_whitespace_is_invalid_not_trimmed(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE leads SET matched_customer_id=' customer-1 ' "
            "WHERE lead_id='lead-1'"
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["reason_counts"] == {"CANONICAL_IDENTITY_INVALID": 1}


def test_meta_identity_whitespace_is_invalid_not_trimmed(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE ad_experiment SET source_campaign_id=' campaign-1 ' "
            "WHERE experiment_id='experiment-1'"
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_meta_event_count"] == 0
    assert "META_ID_INVALID" in report["blocking_reasons"]


def test_study_identity_whitespace_is_invalid_not_trimmed(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    control = {
        "meta_randomization": {
            "study_id": " study-1 ",
            "study_cell_id": "cell-experiment-1",
            "readback_verified": True,
        }
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE ad_experiment SET control_definition_json=? "
            "WHERE experiment_id='experiment-1'",
            (json.dumps(control, sort_keys=True),),
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_meta_event_count"] == 0
    assert "STUDY_ID_INVALID" in report["blocking_reasons"]


def test_snapshot_sha_and_mtime_are_unchanged_and_connection_is_query_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    before_hash = _sha256(db_path)
    before_stat = db_path.stat()

    with open_readonly_snapshot(db_path) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden_write (id TEXT)")

    audit_snapshot(_input(db_path))
    after_stat = db_path.stat()
    assert _sha256(db_path) == before_hash
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_nonempty_snapshot_sidecar_fails_closed(
    tmp_path: Path, suffix: str
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    Path(f"{db_path}{suffix}").write_bytes(b"not-checkpointed")

    with pytest.raises(SourceAuditError, match="SNAPSHOT_SIDECAR_PRESENT"):
        audit_snapshot(_input(db_path))


def test_snapshot_sidecar_drift_during_audit_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    original = audit_module._source_schema

    def create_drifting_sidecar(conn: sqlite3.Connection):
        result = original(conn)
        Path(f"{db_path}-wal").write_bytes(b"appeared-during-audit")
        return result

    monkeypatch.setattr(audit_module, "_source_schema", create_drifting_sidecar)

    with pytest.raises(SourceAuditError, match="SOURCE_SIDECAR_DRIFTED"):
        audit_snapshot(_input(db_path))


def test_duplicate_experiment_ad_mapping_is_ambiguous(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_experiment(conn, experiment_id="experiment-2", ad_id="ad-1")

    report = audit_snapshot(
        _input(db_path, experiments=("experiment-1", "experiment-2"))
    )

    assert report["status"] == "BLOCKED"
    assert report["counts"]["exact_meta_event_count"] == 0
    assert report["ambiguous_reason_counts"] == {
        "AMBIGUOUS_EXPERIMENT_AD_ID": 1
    }


def test_lead_customer_link_conflict_is_ambiguous(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE leads SET matched_customer_id='different-customer' WHERE lead_id='lead-1'"
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["ambiguous_reason_counts"] == {
        "LEAD_CUSTOMER_LINK_CONFLICT": 1
    }


def test_two_canonical_keys_without_complete_crm_relation_are_missing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE leads SET matched_customer_id='' WHERE lead_id='lead-1'"
        )
        conn.execute(
            "UPDATE customer_projection SET lead_id='' WHERE customer_id='customer-1'"
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_identity_event_count"] == 0
    assert report["missing_reason_counts"] == {
        "CANONICAL_IDENTITY_NOT_IN_CRM": 1
    }


def test_report_and_evidence_hashes_are_deterministic(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    audit_input = _input(db_path)

    first = audit_snapshot(audit_input)
    second = audit_snapshot(audit_input)

    assert first == second
    assert first["report_hash"] == second["report_hash"]
    assert first["row_evidence_hash"] == second["row_evidence_hash"]


def test_time_market_and_experiment_boundaries_exclude_unrelated_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_bind(
            conn,
            event_id="outside-window",
            occurred_at="2026-07-01T02:00:00Z",
            raw={"lead_id": "lead-1"},
        )
        raw_json = canonical_json({"lead_id": "lead-1"})
        conn.execute(
            """INSERT INTO tugao_bind_success_raw_events
               (event_id,bind_status,occurred_at_utc,updated_at_utc,business_date,
                project,country,campaign_id,adset_id,ad_id,bind_id,customer_user_id,
                user_key,raw_payload_sha256,raw_payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "other-market",
                "success",
                "2026-08-01T02:00:00Z",
                "2026-08-01T02:00:00Z",
                "2026-08-01",
                "TUGAO",
                "BR",
                "campaign-1",
                "adset-1",
                "ad-1",
                "",
                "",
                "",
                hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                raw_json,
            ),
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["candidate_event_count"] == 1
    assert report["counts"]["exact_identity_event_count"] == 1


def test_candidate_scope_uses_exact_experiment_meta_tuple(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_bind(
            conn,
            event_id="same-campaign-adset-other-ad",
            raw={"lead_id": "lead-1", "customer_id": "customer-1"},
            ad_id="ad-other",
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["candidate_event_count"] == 1
    assert "AD_NOT_IN_EXPERIMENT" not in report["reason_counts"]


def test_empty_occurred_at_does_not_hide_out_of_window_updated_at(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tugao_bind_success_raw_events "
            "SET occurred_at_utc='', updated_at_utc='2026-07-31T23:59:59Z' "
            "WHERE event_id='event-1'"
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["candidate_event_count"] == 0
    assert "NO_CANDIDATE_EVENTS" in report["blocking_reasons"]


def test_business_date_only_is_not_precise_for_partial_utc_day(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tugao_bind_success_raw_events "
            "SET occurred_at_utc='', updated_at_utc='' WHERE event_id='event-1'"
        )
    audit_input = _input(db_path)
    partial_day_input = AuditInput(
        **{
            **audit_input.__dict__,
            "window_start": "2026-08-01T12:00:00Z",
            "window_end": "2026-08-01T18:00:00Z",
        }
    )

    report = audit_snapshot(partial_day_input)

    assert report["counts"]["candidate_event_count"] == 1
    assert report["counts"]["exact_meta_event_count"] == 0
    assert report["reason_counts"] == {"EVENT_TIME_PRECISION_INSUFFICIENT": 1}


def test_missing_study_id_blocks_exact_meta_chain(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    control = {
        "meta_randomization": {
            "study_cell_id": "cell-experiment-1",
            "readback_verified": True,
        }
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE ad_experiment SET control_definition_json=? "
            "WHERE experiment_id='experiment-1'",
            (json.dumps(control, sort_keys=True),),
        )

    report = audit_snapshot(_input(db_path))

    assert report["counts"]["exact_meta_event_count"] == 0
    assert report["missing_reason_counts"] == {"MISSING_STUDY_ID": 1}


def test_selected_experiments_must_share_one_study_id(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_experiment(
            conn,
            experiment_id="experiment-2",
            ad_id="ad-2",
            study_id="study-2",
        )

    report = audit_snapshot(
        _input(db_path, experiments=("experiment-1", "experiment-2"))
    )

    assert "STUDY_ID_NOT_SHARED" in report["blocking_reasons"]


def test_selected_experiments_require_unique_study_cells(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_experiment(
            conn,
            experiment_id="experiment-2",
            ad_id="ad-2",
            study_cell_id="cell-experiment-1",
        )

    report = audit_snapshot(
        _input(db_path, experiments=("experiment-1", "experiment-2"))
    )

    assert "DUPLICATE_STUDY_CELL_ID" in report["blocking_reasons"]


def test_max_events_plus_one_blocks_without_truncating(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_bind(
            conn,
            event_id="event-2",
            raw={"lead_id": "lead-1", "customer_id": "customer-1"},
        )

    report = audit_snapshot(_input(db_path, max_events=1))

    assert report["status"] == "BLOCKED"
    assert "SOURCE_LIMIT_EXCEEDED" in report["blocking_reasons"]
    assert report["counts"]["candidate_event_count"] == 0


def test_missing_required_schema_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (id TEXT)")

    with pytest.raises(SourceAuditError, match="SOURCE_SCHEMA_MISSING"):
        audit_snapshot(_input(db_path))


def test_cli_outputs_only_redacted_canonical_report_and_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)

    exit_code = cli_main(
        [
            "--db-path",
            str(db_path),
            "--expected-db-sha256",
            _sha256(db_path),
            "--account-id",
            "act-1",
            "--market",
            "MX",
            "--experiment-id",
            "experiment-1",
            "--window-start",
            WINDOW_START,
            "--window-end",
            WINDOW_END,
            "--project",
            "TUGAO",
            "--max-events",
            "100",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert captured.out.strip() == canonical_json(payload)
    assert "lead-1" not in captured.out
    assert "customer-1" not in captured.out
    assert "raw_payload" not in captured.out
    assert payload["blocking_reasons"] == [
        "QUALIFICATION_RULE_UNFROZEN",
        "READBACK_PROVENANCE_UNAUDITED",
    ]


def test_corrupt_sqlite_is_redacted_and_cli_returns_66(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "corrupt.sqlite3"
    db_path.write_bytes(b"not-a-sqlite-database")

    exit_code = cli_main(
        [
            "--db-path",
            str(db_path),
            "--expected-db-sha256",
            _sha256(db_path),
            "--account-id",
            "act-1",
            "--market",
            "MX",
            "--experiment-id",
            "experiment-1",
            "--window-start",
            WINDOW_START,
            "--window-end",
            WINDOW_END,
            "--project",
            "TUGAO",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 66
    assert captured.out == ""
    assert captured.err == "SOURCE_SQLITE_ERROR\n"
    assert str(db_path) not in captured.err


def test_experiment_count_above_canary_limit_returns_64(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    experiment_args = [
        value
        for index in range(33)
        for value in ("--experiment-id", f"experiment-{index}")
    ]

    exit_code = cli_main(
        [
            "--db-path",
            str(db_path),
            "--expected-db-sha256",
            _sha256(db_path),
            "--account-id",
            "act-1",
            "--market",
            "MX",
            *experiment_args,
            "--window-start",
            WINDOW_START,
            "--window-end",
            WINDOW_END,
            "--project",
            "TUGAO",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 64
    assert captured.out == ""
    assert captured.err == "EXPERIMENT_COUNT_INVALID\n"


def test_exit_zero_means_only_a_complete_tool_audit() -> None:
    assert exit_code_for_report({"status": "COMPLETE", "blocking_reasons": []}) == 0
    assert exit_code_for_report(
        {"status": "BLOCKED", "blocking_reasons": ["ANY_BLOCKER"]}
    ) == 2


def test_snapshot_hash_mismatch_fails_before_open(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite3"
    _create_snapshot(db_path)
    audit_input = _input(db_path)
    bad_input = AuditInput(
        **{**audit_input.__dict__, "expected_db_sha256": "0" * 64}
    )

    with pytest.raises(SourceAuditError, match="SNAPSHOT_SHA256_MISMATCH"):
        audit_snapshot(bad_input)
