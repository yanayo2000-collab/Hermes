from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Dict, Iterable

from app.growth.audience_strategy import (
    AUDIENCE_TARGETING_SEARCH_TERMS,
    audience_strategy,
    strict_meta_targeting,
)
from app.growth.common import canonical_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthValidationError
from app.growth.schema import ensure_growth_schema


class MetaAudiencePreflightService:
    """Server-owned, GET-only evidence for one audience split-test Plan."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        session: Any,
        access_token: str,
        graph_root: str,
        business_ids: Iterable[str],
        application_id: str,
        store_url: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.conn = conn
        ensure_growth_schema(conn)
        self.session = session
        self.access_token = str(access_token or "").strip()
        self.graph_root = str(graph_root or "").rstrip("/")
        self.business_ids = tuple(str(item or "").strip() for item in business_ids if str(item or "").strip())
        self.application_id = str(application_id or "").strip()
        self.store_url = str(store_url or "").strip()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds or 20), 29.0))

    def run(self, launch_id: str) -> Dict[str, Any]:
        if not self.session or not self.access_token or not self.graph_root:
            raise GrowthValidationError("meta_audience_preflight_unavailable")
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code",
            (str(launch_id or "").strip(),),
        ).fetchall()
        if len(rows) != 2:
            raise GrowthValidationError("audience_experiment_pair_required")
        account_ids = {str(row["account_id"] or "").removeprefix("act_") for row in rows}
        countries = {str(row["country"] or "").upper() for row in rows}
        if len(account_ids) != 1 or len(countries) != 1:
            raise GrowthValidationError("launch_experiments_must_share_account_and_country")
        account_id = next(iter(account_ids))
        country = next(iter(countries))
        strategies = []
        test_variables = set()
        for row in rows:
            hypothesis = json.loads(str(row["hypothesis_json"] or "{}"))
            test_variables.add(str(hypothesis.get("test_variable") or "").strip().lower())
            key = str(dict(hypothesis.get("audience_strategy") or {}).get("strategy_key") or "").upper()
            if not key and "copy_variant" not in test_variables:
                raise GrowthValidationError("launch_is_not_audience_experiment")
            strategies.append(key or "BROAD")
        copy_test = test_variables == {"copy_variant"}
        if not copy_test and (len(set(strategies)) != 2 or "BROAD" not in strategies):
            raise GrowthValidationError("audience_experiment_pair_not_allowed")
        if copy_test:
            strategies = ["BROAD", "BROAD"]

        permissions = self._get("me/permissions", params={})
        granted = {
            str(item.get("permission") or "").lower()
            for item in list(permissions.get("data") or [])
            if str(item.get("status") or "").lower() == "granted"
        }
        if "ads_management" not in granted:
            raise GrowthValidationError("meta_ads_management_permission_required")
        account = self._get(
            f"act_{account_id}",
            params={"fields": "id,name,account_status"},
        )
        if int(account.get("account_status") or 0) != 1:
            raise GrowthValidationError("meta_account_not_eligible")
        business_id = ""
        for candidate in self.business_ids:
            try:
                business = self._get(candidate, params={"fields": "id,name"})
            except GrowthValidationError:
                continue
            if str(business.get("id") or "") == candidate:
                business_id = candidate
                break
        if not business_id:
            raise GrowthValidationError("meta_business_access_required_for_split_test")

        targeting_terms: Dict[str, str] = {}
        for key in strategies:
            targeting_terms.update({
                str(target_id): str(query)
                for target_id, query in dict(AUDIENCE_TARGETING_SEARCH_TERMS.get(key) or {}).items()
            })
        self._validate_targeting_ids(targeting_terms)

        delivery: Dict[str, Dict[str, Any]] = {}
        if copy_test:
            broad_delivery = self._delivery_estimate(account_id, strict_meta_targeting(country, "BROAD"))
            delivery = {"C1": dict(broad_delivery), "C2": dict(broad_delivery)}
            intersection = dict(broad_delivery)
            overlap_ratio = 1.0
        else:
            for key in strategies:
                delivery[key] = self._delivery_estimate(
                    account_id, strict_meta_targeting(country, key),
                )
            intersection_targeting = self._intersection_targeting(country, strategies)
            intersection = self._delivery_estimate(account_id, intersection_targeting)
            smaller_lower = min(int(delivery[key]["lower"]) for key in strategies)
            overlap_ratio = round(int(intersection["lower"]) / smaller_lower, 6) if smaller_lower > 0 else None
        if overlap_ratio is None:
            raise GrowthValidationError("meta_audience_overlap_unavailable")

        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=2)
        end = start + timedelta(days=7)
        expires = now + timedelta(minutes=55)
        preflight_id = new_id("audpreflight")
        evidence = {
            "preflight_id": preflight_id,
            "launch_id": str(launch_id),
            "source": "meta_graph_read_only",
            "status": "VERIFIED",
            "account_id": account_id,
            "account_name": str(account.get("name") or ""),
            "business_id": business_id,
            "country": country,
            "test_variable": "copy_variant" if copy_test else "audience_strategy",
            "strategy_keys": strategies,
            "targeting_ids": sorted(targeting_terms),
            "delivery_estimates": delivery,
            "intersection_estimate": intersection,
            "overlap_ratio": overlap_ratio,
            "checked_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "meta_writes_performed": False,
        }
        digest = payload_hash(evidence)
        with self.conn:
            self.conn.execute(
                """INSERT INTO ad_audience_preflight
                (preflight_id,launch_id,account_id,business_id,country,strategy_keys_json,
                 evidence_json,evidence_hash,status,checked_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?, 'VERIFIED',?,?)""",
                (preflight_id, str(launch_id), account_id, business_id, country,
                 canonical_json(strategies), canonical_json(evidence), digest,
                 evidence["checked_at"], evidence["expires_at"]),
            )
        return {**evidence, "evidence_hash": digest}

    def _delivery_estimate(self, account_id: str, targeting: Dict[str, Any]) -> Dict[str, Any]:
        body = self._get(
            f"act_{account_id}/delivery_estimate",
            params={
                "optimization_goal": "APP_INSTALLS",
                "promoted_object": canonical_json({
                    "application_id": self.application_id,
                    "object_store_url": self.store_url,
                }),
                "targeting_spec": canonical_json(targeting),
            },
        )
        item = dict(next(iter(list(body.get("data") or [])), {}) or {})
        if item.get("estimate_ready") is False:
            raise GrowthValidationError("meta_delivery_estimate_not_ready")
        lower = int(item.get("estimate_mau_lower_bound") or 0)
        upper = int(item.get("estimate_mau_upper_bound") or 0)
        if lower <= 0 or upper < lower:
            raise GrowthValidationError("meta_delivery_estimate_invalid")
        return {"lower": lower, "upper": upper, "estimate_ready": True}

    def _validate_targeting_ids(self, targeting_terms: Dict[str, str]) -> None:
        for target_id, target_name in targeting_terms.items():
            if not target_id or not target_name:
                raise GrowthValidationError("meta_targeting_registry_incomplete")
            body = self._get(
                "search",
                params={"type": "adinterest", "q": target_name, "limit": 50},
            )
            found = {str(item.get("id") or "") for item in list(body.get("data") or [])}
            if target_id not in found:
                raise GrowthValidationError(f"meta_targeting_id_not_found:{target_id}")

    @staticmethod
    def _intersection_targeting(country: str, strategies: list[str]) -> Dict[str, Any]:
        detailed_blocks = []
        for key in strategies:
            for block in list(audience_strategy(key).get("detailed_targeting", {}).get("flexible_spec") or []):
                detailed_blocks.append(deepcopy(block))
        result = strict_meta_targeting(country, "BROAD")
        if detailed_blocks:
            result["flexible_spec"] = detailed_blocks
        return result

    def _get(self, path: str, *, params: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.graph_root}/{str(path).lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
            body = dict(response.json() or {})
        except Exception as exc:
            raise GrowthValidationError("meta_audience_preflight_graph_failed") from exc
        if body.get("error"):
            raise GrowthValidationError("meta_audience_preflight_graph_failed")
        return body
