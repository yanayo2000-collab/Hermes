from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional


_DIRECTION_RE = re.compile(r"^([A-Z]{2,4})_ST_H1_V1(?:_\d{6}-\d{2})?(?:-L[A-Z0-9]{5})?(?:-R\d+)?$")
_DIRECTION_CODE_BY_ANGLE = {
    "网赚效率": "PR",
    "安全合规": "SC",
    "流程透明": "ES",
    "私聊顾问": "GT",
}


def compact_launch_ad_name(
    direction_code: str, naming_date: str, variant: int, *, launch_id: str = "", retry: int = 1,
) -> str:
    short_date = str(naming_date).replace("-", "")[2:8]
    if len(short_date) != 6 or not short_date.isdigit():
        raise ValueError("naming_date_must_be_yyyymmdd")
    code = str(direction_code or "EXP").strip().upper()
    if not re.fullmatch(r"[A-Z]{2,4}", code):
        raise ValueError("direction_code_invalid")
    name = f"{code}_ST_H1_V1_{short_date}-{int(variant):02d}"
    normalized_launch_id = str(launch_id or "").strip().upper()
    if normalized_launch_id:
        suffix = re.sub(r"[^A-Z0-9]", "", normalized_launch_id)[-5:]
        if not re.fullmatch(r"[A-Z0-9]{5}", suffix):
            raise ValueError("launch_id_suffix_invalid")
        name = f"{name}-L{suffix}"
    return f"{name}-R{int(retry)}" if int(retry) > 1 else name


def _json(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _date_from_experiment(row: sqlite3.Row, hypothesis: Dict[str, Any]) -> str:
    naming_date = str(hypothesis.get("naming_date") or "").replace("-", "")
    if len(naming_date) == 8 and naming_date.isdigit():
        return naming_date
    campaign = str((_json(row["variant_definition_json"]).get("meta_names") or {}).get("campaign") or "")
    match = re.search(r"_(\d{6})$", campaign)
    if match:
        return "20" + match.group(1)
    return datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).strftime("%Y%m%d")


def launch_experiment_name(conn: sqlite3.Connection, experiment_id: str) -> Optional[Dict[str, Any]]:
    if not experiment_id or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_experiment'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT experiment_id, source_report_id, hypothesis_json, variant_definition_json, created_at "
        "FROM ad_experiment WHERE experiment_id = ?", (experiment_id,),
    ).fetchone()
    if not row:
        return None
    hypothesis = _json(row["hypothesis_json"])
    variant_definition = _json(row["variant_definition_json"])
    direction = dict(hypothesis.get("creative_direction") or variant_definition.get("creative_direction") or {})
    meta_names = dict(hypothesis.get("meta_names") or variant_definition.get("meta_names") or {})
    match = _DIRECTION_RE.match(str(meta_names.get("ad") or ""))
    creative_angle = str(hypothesis.get("creative_angle") or variant_definition.get("creative_angle") or "").strip()
    matched_code = match.group(1) if match and match.group(1) != "EXP" else ""
    code = str(direction.get("code") or matched_code or _DIRECTION_CODE_BY_ANGLE.get(creative_angle) or "EXP").upper()
    return {
        "experiment_id": experiment_id,
        "launch_id": str(row["source_report_id"] or ""),
        "direction_code": code,
        "variant": int(hypothesis.get("variant") or variant_definition.get("variant") or 1),
        "naming_date": _date_from_experiment(row, hypothesis),
        "hypothesis": hypothesis,
        "variant_definition": variant_definition,
        "meta_names": meta_names,
    }


def next_launch_creative_name(conn: sqlite3.Connection, *, launch_id: str, growth_experiment_id: str) -> Optional[Dict[str, Any]]:
    info = launch_experiment_name(conn, growth_experiment_id)
    if not info or not launch_id or info["launch_id"] != launch_id:
        return None
    retry = 1
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_pro_work_queue'").fetchone():
        retry += int(conn.execute(
            "SELECT COUNT(*) FROM creative_pro_work_queue "
            "WHERE json_extract(material_refs_json, '$.launch_id') = ? "
            "AND json_extract(material_refs_json, '$.growth_experiment_id') = ?",
            (launch_id, growth_experiment_id),
        ).fetchone()[0])
    return {
        **info,
        "retry": retry,
        "ad_name": compact_launch_ad_name(
            info["direction_code"], info["naming_date"], info["variant"], launch_id=launch_id, retry=retry,
        ),
    }


def backfill_launch_creative_names(conn: sqlite3.Connection, *, apply: bool = False) -> Dict[str, Any]:
    experiments = conn.execute(
        "SELECT experiment_id FROM ad_experiment WHERE source_report_id LIKE 'newacct_%' ORDER BY created_at, experiment_id"
    ).fetchall()
    experiment_updates, job_updates = [], []
    for experiment_row in experiments:
        info = launch_experiment_name(conn, str(experiment_row["experiment_id"]))
        if not info:
            continue
        base_name = compact_launch_ad_name(
            info["direction_code"], info["naming_date"], info["variant"], launch_id=info["launch_id"],
        )
        hypothesis, variant_definition = dict(info["hypothesis"]), dict(info["variant_definition"])
        for document in (hypothesis, variant_definition):
            names = dict(document.get("meta_names") or info["meta_names"])
            names["ad"] = base_name
            document["meta_names"] = names
            document["creative_name_version"] = "launch_date_order_sequence_v2"
        experiment_updates.append({"experiment_id": info["experiment_id"], "ad_name": base_name})
        jobs = conn.execute(
            "SELECT job_id, experiment_id, material_refs_json FROM creative_pro_work_queue "
            "WHERE json_extract(material_refs_json, '$.launch_id') = ? "
            "AND json_extract(material_refs_json, '$.growth_experiment_id') = ? ORDER BY created_at, job_id",
            (info["launch_id"], info["experiment_id"]),
        ).fetchall()
        for retry, job in enumerate(jobs, start=1):
            material = _json(job["material_refs_json"])
            job_name = compact_launch_ad_name(
                info["direction_code"], info["naming_date"], info["variant"],
                launch_id=info["launch_id"], retry=retry,
            )
            names = dict(material.get("meta_names") or info["meta_names"])
            names["ad"] = job_name
            material.update({"ad": job_name, "meta_names": names, "creative_name_version": "launch_date_order_sequence_v2", "creative_retry": retry})
            job_updates.append({"job_id": job["job_id"], "ad_name": job_name})
            if apply:
                conn.execute("UPDATE creative_pro_work_queue SET material_refs_json = ? WHERE job_id = ?", (json.dumps(material, ensure_ascii=False, sort_keys=True), job["job_id"]))
                suggestion = conn.execute("SELECT payload_json FROM creative_experiment_suggestions WHERE experiment_id = ?", (job["experiment_id"],)).fetchone()
                if suggestion:
                    payload = _json(suggestion["payload_json"])
                    payload["ad"] = job_name
                    task = dict(payload.get("production_task") or {})
                    task_names = dict(task.get("meta_names") or names)
                    task_names["ad"] = job_name
                    task.update({"ad": job_name, "meta_names": task_names})
                    payload["production_task"] = task
                    conn.execute("UPDATE creative_experiment_suggestions SET payload_json = ? WHERE experiment_id = ?", (json.dumps(payload, ensure_ascii=False, sort_keys=True), job["experiment_id"]))
        if apply:
            conn.execute(
                "UPDATE ad_experiment SET hypothesis_json = ?, variant_definition_json = ? WHERE experiment_id = ?",
                (json.dumps(hypothesis, ensure_ascii=False, sort_keys=True), json.dumps(variant_definition, ensure_ascii=False, sort_keys=True), info["experiment_id"]),
            )
    if apply:
        conn.commit()
    return {"apply": apply, "experiment_count": len(experiment_updates), "job_count": len(job_updates), "experiments": experiment_updates, "jobs": job_updates}
