from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from app.growth.appsflyer_meta_cell_lineage import (
    CEILING,
    CellLineageEvidenceError,
    capture_meta_graph,
    derive_lineage_evidence,
    json_bytes,
    parse_request,
    validate_lineage_evidence,
)


HEADER = (
    "Media source,Ad ID,Impressions,Clicks,Total attributions appsflyer,"
    "Installs appsflyer,Re-attributions appsflyer,Re-engagements appsflyer\n"
)


def csv_bytes(*, duplicate_c1: bool = False, omit_c2: bool = False) -> bytes:
    rows = [
        "Facebook Ads,120250588945870544,505,10,2,2,,\n",
    ]
    if duplicate_c1:
        rows.append("Facebook Ads,120250588945870544,1,1,1,1,0,0\n")
    if not omit_c2:
        rows.append("Facebook Ads,120250588946840544,793,22,9,9,0,0\n")
    return (HEADER + "".join(rows)).encode("utf-8")


def request_for(raw: bytes, *, mode: str = "HISTORICAL_TEST", timezone_name: str = "Asia/Hong_Kong") -> dict:
    return {
        "schema_version": "gle-af-meta-cell-lineage-request-v1",
        "evidence_id": "gle-lineage-historical-20260810",
        "mode": mode,
        "requested_at": "2026-08-10T03:35:12+00:00",
        "appsflyer_raw_sha256": hashlib.sha256(raw).hexdigest(),
        "report_window": {
            "date_from": "2026-07-11",
            "date_to": "2026-08-09",
            "reporting_timezone": timezone_name,
            "data_cutoff_at": "2026-08-10T03:35:00+00:00",
        },
        "subject": {
            "app_id": "com.timetrade.duitan",
            "account_id": "1012060198097836",
            "market": "MX",
            "study_id": "1755195762483275",
            "campaign_id": "120250588944820544",
            "cells": [
                {
                    "cell_key": "C1",
                    "study_cell_id": "1657983691931915",
                    "adset_id": "120250588945530544",
                    "ad_id": "120250588945870544",
                },
                {
                    "cell_key": "C2",
                    "study_cell_id": "1587562426321061",
                    "adset_id": "120250588946480544",
                    "ad_id": "120250588946840544",
                },
            ],
        },
    }


def meta_capture() -> dict:
    return {
        "graph_api_version": "v25.0",
        "captured_at": "2026-08-10T03:35:30+00:00",
        "study": {
            "id": "1755195762483275",
            "type": "SPLIT_TEST",
            "start_time": "2026-08-06T09:10:20+00:00",
            "end_time": "2026-08-13T09:10:20+00:00",
            "observation_end_time": "2026-08-13T09:10:20+00:00",
        },
        "cells": [
            {
                "id": "1657983691931915",
                "treatment_percentage": 50,
                "control_percentage": 0,
                "ad_entities_count": 1,
                "adsets": [
                    {"id": "120250588945530544", "campaign_id": "120250588944820544"}
                ],
            },
            {
                "id": "1587562426321061",
                "treatment_percentage": 50,
                "control_percentage": 0,
                "ad_entities_count": 1,
                "adsets": [
                    {"id": "120250588946480544", "campaign_id": "120250588944820544"}
                ],
            },
        ],
        "ads": [
            {
                "id": "120250588945870544",
                "account_id": "1012060198097836",
                "campaign_id": "120250588944820544",
                "adset_id": "120250588945530544",
            },
            {
                "id": "120250588946840544",
                "account_id": "1012060198097836",
                "campaign_id": "120250588944820544",
                "adset_id": "120250588946480544",
            },
        ],
    }


def test_historical_exact_lineage_round_trip_and_ceiling() -> None:
    raw = csv_bytes()
    request = request_for(raw)
    evidence = derive_lineage_evidence(
        request=request,
        appsflyer_raw=raw,
        meta_capture=meta_capture(),
    )

    assert evidence["status"] == "HISTORICAL_EXACT_CELL_LINEAGE_REDERIVED"
    assert [row["cell_key"] for row in evidence["rows"]] == ["C1", "C2"]
    assert [row["appsflyer"]["installs"] for row in evidence["rows"]] == [2, 9]
    assert evidence["appsflyer_raw_rows"] == 2
    assert evidence["meta_capture_hash"] == hashlib.sha256(
        json.dumps(
            evidence["meta_capture"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert evidence["ceiling"] == CEILING
    assert evidence["ceiling"]["source_content_authority"] == "NOT_VERIFIED"
    assert evidence["ceiling"]["gate0_result_effect"] == "UNCHANGED"
    assert evidence["ceiling"]["snapshot_emitted"] is False
    assert "HISTORICAL_WINDOW_NOT_ADMISSIBLE_FOR_NATURAL_AUDIT" in evidence["gaps"]
    assert validate_lineage_evidence(
        evidence,
        request=request,
        appsflyer_raw=raw,
        meta_capture=meta_capture(),
    ) == evidence


def test_request_requires_canonical_unique_json() -> None:
    raw = csv_bytes()
    request = request_for(raw)
    assert parse_request(json_bytes(request)) == request
    with pytest.raises(CellLineageEvidenceError, match="G104B6_REQUEST_NOT_CANONICAL"):
        parse_request(json.dumps(request, indent=2).encode())
    with pytest.raises(CellLineageEvidenceError, match="G104B6_JSON_DUPLICATE_KEY"):
        parse_request(b'{"schema_version":"x","schema_version":"y"}\n')


def test_unhashable_external_shapes_use_stable_error_contract() -> None:
    raw = csv_bytes()
    request = request_for(raw)
    request["mode"] = []
    with pytest.raises(CellLineageEvidenceError, match="G104B6_MODE_INVALID"):
        derive_lineage_evidence(
            request=request,
            appsflyer_raw=raw,
            meta_capture=meta_capture(),
        )

    request = request_for(raw)
    capture = meta_capture()
    capture["cells"][0]["id"] = []
    with pytest.raises(CellLineageEvidenceError, match="G104B6_META_CELL_ID_INVALID"):
        derive_lineage_evidence(
            request=request,
            appsflyer_raw=raw,
            meta_capture=capture,
        )


def test_natural_candidate_requires_asia_shanghai() -> None:
    raw = csv_bytes()
    with pytest.raises(CellLineageEvidenceError, match="G104B6_NATURAL_TIMEZONE_INVALID"):
        derive_lineage_evidence(
            request=request_for(raw, mode="NATURAL_AUDIT_CANDIDATE"),
            appsflyer_raw=raw,
            meta_capture=meta_capture(),
        )
    natural = request_for(
        raw,
        mode="NATURAL_AUDIT_CANDIDATE",
        timezone_name="Asia/Shanghai",
    )
    result = derive_lineage_evidence(
        request=natural,
        appsflyer_raw=raw,
        meta_capture=meta_capture(),
    )
    assert result["status"] == "NATURAL_AUDIT_CANDIDATE_EXACT_CELL_LINEAGE_REDERIVED"
    assert "HISTORICAL_WINDOW_NOT_ADMISSIBLE_FOR_NATURAL_AUDIT" not in result["gaps"]
    assert result["ceiling"]["source_content_authority"] == "NOT_VERIFIED"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (csv_bytes(duplicate_c1=True), "G104B6_TARGET_AD_GRAIN_INVALID"),
        (csv_bytes(omit_c2=True), "G104B6_TARGET_AD_GRAIN_INVALID"),
    ],
)
def test_csv_exact_target_grain_is_fail_closed(raw: bytes, code: str) -> None:
    request = request_for(raw)
    with pytest.raises(CellLineageEvidenceError, match=code):
        derive_lineage_evidence(
            request=request,
            appsflyer_raw=raw,
            meta_capture=meta_capture(),
        )


def test_csv_external_raw_sha_is_required() -> None:
    raw = csv_bytes()
    request = request_for(raw)
    request["appsflyer_raw_sha256"] = "0" * 64
    with pytest.raises(CellLineageEvidenceError, match="G104B6_CSV_SHA_MISMATCH"):
        derive_lineage_evidence(
            request=request,
            appsflyer_raw=raw,
            meta_capture=meta_capture(),
        )


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda value: value["cells"][0]["adsets"][0].__setitem__("id", "wrong-adset"),
            "G104B6_META_CELL_ADSET_BINDING_INVALID",
        ),
        (
            lambda value: value["ads"][0].__setitem__("account_id", "wrong-account"),
            "G104B6_META_AD_BINDING_INVALID",
        ),
        (
            lambda value: value["study"].__setitem__("type", "BRAND_LIFT"),
            "G104B6_STUDY_BINDING_INVALID",
        ),
    ],
)
def test_meta_cross_bindings_fail_closed(mutator, code: str) -> None:
    raw = csv_bytes()
    capture = meta_capture()
    mutator(capture)
    with pytest.raises(CellLineageEvidenceError, match=code):
        derive_lineage_evidence(
            request=request_for(raw),
            appsflyer_raw=raw,
            meta_capture=capture,
        )


def test_full_rehash_cannot_promote_ceiling() -> None:
    raw = csv_bytes()
    request = request_for(raw)
    evidence = derive_lineage_evidence(
        request=request,
        appsflyer_raw=raw,
        meta_capture=meta_capture(),
    )
    forged = copy.deepcopy(evidence)
    forged["ceiling"]["source_content_authority"] = "VERIFIED"
    forged["ceiling"]["gate0_result_effect"] = "CONTROLLED_FEASIBLE"
    forged["evidence_hash"] = ""
    body = {key: value for key, value in forged.items() if key != "evidence_hash"}
    forged["evidence_hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(CellLineageEvidenceError, match="G104B6_EVIDENCE_REDERIVE_MISMATCH"):
        validate_lineage_evidence(
            forged,
            request=request,
            appsflyer_raw=raw,
            meta_capture=meta_capture(),
        )


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def iter_content(self, *, chunk_size: int):
        raw = json.dumps(self.payload, separators=(",", ":")).encode()
        for offset in range(0, len(raw), chunk_size):
            yield raw[offset:offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self) -> None:
        capture = meta_capture()
        self.responses = {
            "1755195762483275": capture["study"],
            "1755195762483275/cells": {"data": [
                {key: value for key, value in cell.items() if key != "adsets"}
                for cell in capture["cells"]
            ]},
            "1657983691931915/adsets": {"data": capture["cells"][0]["adsets"]},
            "1587562426321061/adsets": {"data": capture["cells"][1]["adsets"]},
            "120250588945870544": capture["ads"][0],
            "120250588946840544": capture["ads"][1],
        }
        self.calls: list[tuple[str, dict, dict]] = []
        self.trust_env = True
        self.closed = False

    def get(
        self,
        url: str,
        *,
        params: dict,
        headers: dict,
        timeout: int,
        allow_redirects: bool,
        stream: bool,
    ) -> FakeResponse:
        self.calls.append((url, dict(params), dict(headers)))
        assert timeout == 20
        assert allow_redirects is False
        assert stream is True
        key = url.split("/v25.0/", 1)[1]
        return FakeResponse(self.responses[key])

    def close(self) -> None:
        self.closed = True


def test_meta_capture_uses_only_frozen_get_surface() -> None:
    raw = csv_bytes()
    session = FakeSession()
    capture = capture_meta_graph(
        session=session,
        access_token="secret-not-output",
        request=request_for(raw),
        captured_at="2026-08-10T03:35:30+00:00",
    )
    assert len(session.calls) == 6
    assert all(url.startswith("https://graph.facebook.com/v25.0/") for url, _, _ in session.calls)
    assert all("access_token" not in params for _, params, _ in session.calls)
    assert all(
        headers == {"Authorization": "Bearer secret-not-output"}
        for _, _, headers in session.calls
    )
    assert "secret-not-output" not in json.dumps(capture)


def test_meta_capture_rejects_incomplete_or_extra_cell_pages() -> None:
    raw = csv_bytes()
    session = FakeSession()
    session.responses["1755195762483275/cells"]["paging"] = {
        "next": "https://graph.facebook.com/next"
    }
    with pytest.raises(CellLineageEvidenceError, match="G104B6_META_PAGE_INCOMPLETE"):
        capture_meta_graph(
            session=session,
            access_token="secret-not-output",
            request=request_for(raw),
            captured_at="2026-08-10T03:35:30+00:00",
        )

    session = FakeSession()
    session.responses["1755195762483275/cells"]["data"].append(
        copy.deepcopy(session.responses["1755195762483275/cells"]["data"][0])
    )
    with pytest.raises(CellLineageEvidenceError, match="G104B6_META_CELL_SET_INVALID"):
        capture_meta_graph(
            session=session,
            access_token="secret-not-output",
            request=request_for(raw),
            captured_at="2026-08-10T03:35:30+00:00",
        )


def test_cli_writes_new_mode_600_artifact_and_returns_two(tmp_path: Path, monkeypatch, capsys) -> None:
    raw = csv_bytes()
    request = request_for(raw)
    now = datetime.now(timezone.utc).isoformat()
    request["requested_at"] = now
    request["report_window"]["data_cutoff_at"] = now
    request_path = tmp_path / "request.json"
    csv_path = tmp_path / "source.csv"
    output_path = tmp_path / "evidence.json"
    request_path.write_bytes(json_bytes(request))
    csv_path.write_bytes(raw)
    request_path.chmod(0o600)
    csv_path.chmod(0o600)

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "build_gle_appsflyer_meta_cell_lineage.py"
    spec = importlib.util.spec_from_file_location("build_gle_appsflyer_meta_cell_lineage", script_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    fake = FakeSession()
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", "secret-not-output")
    monkeypatch.setattr(cli.requests, "Session", lambda: fake)
    monkeypatch.setattr(sys, "argv", [
        str(script_path),
        "--request", str(request_path),
        "--appsflyer-csv", str(csv_path),
        "--output", str(output_path),
        "--expected-request-sha256", hashlib.sha256(request_path.read_bytes()).hexdigest(),
    ])
    assert cli.main() == 2
    stdout = capsys.readouterr().out
    assert "secret-not-output" not in stdout
    assert json.loads(stdout)["source_content_authority"] == "NOT_VERIFIED"
    assert output_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(output_path.read_text())["status"] == "HISTORICAL_EXACT_CELL_LINEAGE_REDERIVED"
    assert fake.trust_env is False
    assert fake.closed is True

    assert cli.main() == 64
    assert "secret-not-output" not in capsys.readouterr().err


def test_static_boundary_has_no_meta_write_method() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/growth/appsflyer_meta_cell_lineage.py").read_text()
    cli = (root / "scripts/build_gle_appsflyer_meta_cell_lineage.py").read_text()
    combined = source + cli
    for marker in ("session.post(", "session.put(", "session.patch(", "session.delete("):
        assert marker not in combined
    assert "sqlite3" not in combined
