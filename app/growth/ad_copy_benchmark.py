from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_LIBRARY_PATH = Path(__file__).resolve().parents[2] / "config" / "growth_ad_copy_benchmarks.json"
CORE_MARKETS = {"BR", "MX", "ID"}


def canonical_market(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    aliases = {
        "BRAZIL": "BR", "BRA": "BR",
        "MEXICO": "MX", "MÉXICO": "MX", "MEX": "MX",
        "INDONESIA": "ID", "IDN": "ID",
    }
    return aliases.get(normalized, normalized if normalized in CORE_MARKETS else "")


def load_ad_copy_benchmark_library(path: Optional[Path] = None) -> Dict[str, Any]:
    source = Path(path or DEFAULT_LIBRARY_PATH)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not str(payload.get("version") or "").startswith("gle_copy_benchmark_v"):
        raise ValueError("invalid_ad_copy_benchmark_version")
    markets = dict(payload.get("markets") or {})
    if set(markets) != CORE_MARKETS:
        raise ValueError("ad_copy_benchmark_markets_incomplete")
    for market, item in markets.items():
        sources = list(dict(item or {}).get("sources") or [])
        if len(sources) < 3:
            raise ValueError(f"ad_copy_benchmark_sources_incomplete:{market}")
        if any(source_item.get("public_performance_available") is not False for source_item in sources):
            raise ValueError(f"external_performance_claim_not_allowed:{market}")
    return payload


def copy_signature(primary_text: Any, headline: Any = "", description: Any = "") -> str:
    canonical = json.dumps(
        {
            "primary_text": " ".join(str(primary_text or "").split()),
            "headline": " ".join(str(headline or "").split()),
            "description": " ".join(str(description or "").split()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def copy_version_id(market: Any, direction: Any, primary_text: Any, headline: Any = "", description: Any = "") -> str:
    normalized_market = canonical_market(market) or "GEN"
    normalized_direction = str(direction or "unknown").strip().lower()
    digest = copy_signature(primary_text, headline, description)[:16]
    return f"copyv1_{normalized_market.lower()}_{normalized_direction}_{digest}"


def _pattern_tags(text: str) -> List[str]:
    lowered = str(text or "").lower()
    patterns = {
        "free_time_hook": ("tempo livre", "ratos libres", "waktu luang"),
        "concrete_tasks": ("taref", "tarea", "tugas"),
        "progress_visibility": ("progres", "avance", "pantau"),
        "reward_visibility": ("recompensa", "punto", "hadiah", "poin"),
        "step_by_step": ("passo", "paso", "langkah", "etapa"),
        "income_claim_risk": ("renda extra", "ganar dinero", "penghasilan tambahan"),
    }
    return [name for name, needles in patterns.items() if any(needle in lowered for needle in needles)]


def _benchmark_tables_available(conn: sqlite3.Connection) -> bool:
    names = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ad_creative_asset','ad_creative_performance_daily')"
        ).fetchall()
    }
    return names == {"ad_creative_asset", "ad_creative_performance_daily"}


def aggregate_internal_copy_performance(
    conn: sqlite3.Connection,
    *,
    as_of: Optional[date] = None,
    lookback_days: int = 90,
) -> Dict[str, Any]:
    """Return read-only, de-duplicated ad-plus-creative copy evidence.

    Multiple asset snapshots can carry the same ad/date metrics. The ranking CTE
    keeps one row per ad/day and prefers the strongest downstream attribution
    plus a non-empty body. Results remain directional because image and copy
    were not independently randomized.
    """
    if not _benchmark_tables_available(conn):
        return {"status": "unavailable", "reason": "creative_performance_tables_missing", "items": []}
    end_date = as_of or date.today()
    days = max(1, min(int(lookback_days or 90), 90))
    start_date = end_date - timedelta(days=days - 1)
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                p.*, a.body_text, a.title_text, a.description_text, a.last_seen_at,
                ROW_NUMBER() OVER (
                    PARTITION BY p.report_date_london, p.ad_id
                    ORDER BY
                        CASE WHEN p.data_quality_status='ad_id_with_downstream_text_match' THEN 0 ELSE 1 END,
                        CASE WHEN TRIM(COALESCE(a.body_text,''))<>'' THEN 0 ELSE 1 END,
                        a.last_seen_at DESC,
                        a.asset_id
                ) AS row_rank,
                COUNT(*) OVER (PARTITION BY p.report_date_london, p.ad_id) AS snapshot_count
            FROM ad_creative_performance_daily p
            JOIN ad_creative_asset a ON a.asset_id=p.asset_id
            WHERE p.report_date_london BETWEEN ? AND ?
        )
        SELECT * FROM ranked WHERE row_rank=1
        ORDER BY report_date_london,ad_id
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    buckets: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "spend": 0.0, "impressions": 0.0, "clicks": 0.0, "installs": 0.0,
        "real_joins": 0, "ad_ids": set(), "dates": set(), "countries": set(),
        "snapshot_rows_deduplicated": 0,
    })
    for row in rows:
        primary_text = str(row["body_text"] or "").strip()
        headline = str(row["title_text"] or "").strip()
        description = str(row["description_text"] or "").strip()
        if not primary_text and not headline:
            continue
        signature = copy_signature(primary_text, headline, description)
        market = canonical_market(row["country"])
        bucket = buckets[f"{market}:{signature}"]
        bucket.update({
            "copy_signature": signature,
            "market": market,
            "primary_text": " ".join(primary_text.split()),
            "headline": " ".join(headline.split()),
            "description": " ".join(description.split()),
        })
        bucket["spend"] += float(row["spend"] or 0)
        bucket["impressions"] += float(row["impressions"] or 0)
        bucket["clicks"] += float(row["clicks"] or 0)
        bucket["installs"] += float(row["installs"] or 0)
        bucket["real_joins"] += int(row["tugao_real_bind_count"] or 0)
        bucket["ad_ids"].add(str(row["ad_id"] or ""))
        bucket["dates"].add(str(row["report_date_london"] or ""))
        bucket["countries"].add(str(row["country"] or ""))
        bucket["snapshot_rows_deduplicated"] += max(int(row["snapshot_count"] or 1) - 1, 0)

    items: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        spend = float(bucket["spend"])
        impressions = float(bucket["impressions"])
        clicks = float(bucket["clicks"])
        installs = float(bucket["installs"])
        real_joins = int(bucket["real_joins"])
        mature = installs >= 100 and real_joins >= 10
        items.append({
            "copy_signature": bucket["copy_signature"],
            "copy_version_id": copy_version_id(
                bucket.get("market"), "historical", bucket.get("primary_text"),
                bucket.get("headline"), bucket.get("description"),
            ),
            "market": bucket.get("market") or "",
            "primary_text": bucket.get("primary_text") or "",
            "headline": bucket.get("headline") or "",
            "description": bucket.get("description") or "",
            "spend": round(spend, 2),
            "impressions": round(impressions),
            "clicks": round(clicks),
            "ctr_pct": round(clicks * 100 / impressions, 2) if impressions else None,
            "installs": round(installs),
            "cpi": round(spend / installs, 2) if installs else None,
            "real_joins": real_joins,
            "real_join_cpa": round(spend / real_joins, 2) if real_joins else None,
            "active_days": len(bucket["dates"]),
            "distinct_ads": len(bucket["ad_ids"]),
            "pattern_tags": _pattern_tags(" ".join([bucket.get("primary_text", ""), bucket.get("headline", "")])),
            "evidence_grade": "mature_directional" if mature else "insufficient_directional",
            "causal_copy_claim_allowed": False,
            "snapshot_rows_deduplicated": int(bucket["snapshot_rows_deduplicated"]),
        })
    items.sort(key=lambda item: (
        0 if item["evidence_grade"] == "mature_directional" else 1,
        -(item["real_joins"] or 0), -(item["installs"] or 0), -(item["ctr_pct"] or 0),
    ))
    return {
        "status": "ok",
        "window": {"from": start_date.isoformat(), "to": end_date.isoformat(), "days": days},
        "deduplicated_ad_day_rows": len(rows),
        "copy_count": len(items),
        "items": items,
        "claim_boundary": "directional_ad_plus_creative_evidence_not_copy_causality",
    }


def build_ad_copy_benchmark_report(
    conn: sqlite3.Connection,
    *,
    as_of: Optional[date] = None,
    lookback_days: int = 90,
    library_path: Optional[Path] = None,
) -> Dict[str, Any]:
    library = load_ad_copy_benchmark_library(library_path)
    return {
        "benchmark_version": library["version"],
        "checked_at": library["checked_at"],
        "evidence_policy": library["evidence_policy"],
        "external_market_references": library["markets"],
        "internal_performance": aggregate_internal_copy_performance(
            conn, as_of=as_of, lookback_days=lookback_days,
        ),
        "meta_writes_performed": False,
    }
