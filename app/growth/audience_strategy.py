from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
from typing import Any, Dict

from app.growth.errors import GrowthValidationError


# Base demographic controls are product policy, not experiment variables.
# Meta locale ids are persisted here so plans never rely on translated labels.
COUNTRY_AUDIENCE_POLICIES: Dict[str, Dict[str, Any]] = {
    "BR": {"country_label": "巴西", "gender": "female", "genders": [2], "age_min": 18, "age_max": 40, "language": "pt_BR", "language_label": "葡萄牙语（巴西）", "locales": [16]},
    "MX": {"country_label": "墨西哥", "gender": "female", "genders": [2], "age_min": 18, "age_max": 40, "language": "es_419", "language_label": "西班牙语（拉美）", "locales": [23]},
    "ID": {"country_label": "印度尼西亚", "gender": "female", "genders": [2], "age_min": 18, "age_max": 40, "language": "id_ID", "language_label": "印度尼西亚语", "locales": [25]},
}

AUDIENCE_STRATEGIES: Dict[str, Dict[str, Any]] = {
    "BROAD": {
        "strategy_key": "BROAD",
        "label": "广泛受众",
        "detailed_targeting": {},
        "meta_targeting_ids": [],
        "verification_status": "VERIFIED_EMPTY_BY_DEFINITION",
    },
    "SIDE_HUSTLE": {
        "strategy_key": "SIDE_HUSTLE",
        "label": "副业与灵活工作",
        # OR inside one block; separate blocks are AND. This deliberately
        # avoids turning every loosely related income keyword into one pool.
        "detailed_targeting": {
            "flexible_spec": [
                {"interests": [
                    {"id": "6003214937861", "name": "个体经营（职业）"},
                    {"id": "6003385106853", "name": "Freelance marketplace"},
                ]},
                {"interests": [
                    {"id": "6003343813828", "name": "远程办公"},
                ]},
            ],
        },
        "meta_targeting_ids": ["6003214937861", "6003385106853", "6003343813828"],
        "verification_status": "VERIFIED_TARGETING_SEARCH_AND_DELIVERY_ESTIMATE",
    },
    "DIGITAL_SELLER": {
        "strategy_key": "DIGITAL_SELLER",
        "label": "数字经营人群",
        "detailed_targeting": {
            "flexible_spec": [
                {"interests": [{"id": "6003221485467", "name": "电子商务（零售）"}]},
                {"interests": [{"id": "6003127206524", "name": "数字营销（市场营销）"}]},
            ],
        },
        "meta_targeting_ids": ["6003221485467", "6003127206524"],
        "verification_status": "VERIFIED_TARGETING_SEARCH_AND_DELIVERY_ESTIMATE",
    },
    "FAMILY_HOME": {
        "strategy_key": "FAMILY_HOME",
        "label": "女性居家人群",
        "detailed_targeting": {
            "flexible_spec": [
                {"interests": [
                    {"id": "6002991239659", "name": "母亲（儿童和育儿）"},
                    {"id": "6003232518610", "name": "育儿（儿童和育儿）"},
                ]},
            ],
        },
        "meta_targeting_ids": ["6002991239659", "6003232518610"],
        "verification_status": "VERIFIED_TARGETING_SEARCH_AND_DELIVERY_ESTIMATE",
    },
}

AUDIENCE_TARGETING_SEARCH_TERMS: Dict[str, Dict[str, str]] = {
    "BROAD": {},
    "SIDE_HUSTLE": {
        "6003214937861": "Self-employment",
        "6003385106853": "Freelance marketplace",
        "6003343813828": "Remote work",
    },
    "DIGITAL_SELLER": {
        "6003221485467": "E-commerce",
        "6003127206524": "Digital marketing",
    },
    "FAMILY_HOME": {
        "6002991239659": "Motherhood",
        "6003232518610": "Parenting",
    },
}


# Read-only Meta delivery-estimate evidence captured under the exact fixed
# female 18-40 / country-locale / Android targeting contract. Estimates are
# directional snapshots, not durable promises of delivery.
AUDIENCE_DELIVERY_ESTIMATE_SNAPSHOT: Dict[str, Dict[str, Dict[str, int]]] = {
    "BR": {
        "BROAD": {"lower": 21300000, "upper": 25100000},
        "SIDE_HUSTLE": {"lower": 176900, "upper": 208100},
        "DIGITAL_SELLER": {"lower": 3500000, "upper": 4200000},
        "FAMILY_HOME": {"lower": 10700000, "upper": 12600000},
    },
    "MX": {
        "BROAD": {"lower": 21400000, "upper": 25200000},
        "SIDE_HUSTLE": {"lower": 665400, "upper": 782800},
        "DIGITAL_SELLER": {"lower": 1400000, "upper": 1700000},
        "FAMILY_HOME": {"lower": 9700000, "upper": 11400000},
    },
    "ID": {
        "BROAD": {"lower": 40400000, "upper": 47600000},
        "SIDE_HUSTLE": {"lower": 244500, "upper": 287700},
        "DIGITAL_SELLER": {"lower": 1800000, "upper": 2200000},
        "FAMILY_HOME": {"lower": 16200000, "upper": 19100000},
    },
}


# Do not put these audiences into ordinary sibling Ad Sets: the read-only
# intersection estimates showed material overlap. They become selectable only
# in a pairwise randomized audience experiment with one frozen winning creative.
INITIAL_AUDIENCE_EXPERIMENT_POLICY: Dict[str, Any] = {
    "test_variable": "audience_strategy",
    "required_creative_state": "FROZEN_WINNER",
    "execution_mode": "PAIRWISE_RANDOMIZED",
    "initial_br_rounds": [
        {"baseline": "BROAD", "challenger": "DIGITAL_SELLER"},
        {"baseline": "BROAD", "challenger": "FAMILY_HOME"},
    ],
    "held_strategies": {
        "SIDE_HUSTLE": "BR delivery estimate is below the initial broad-test floor",
    },
}


def country_audience_policy(country: str) -> Dict[str, Any]:
    code = str(country or "").strip().upper()
    policy = COUNTRY_AUDIENCE_POLICIES.get(code)
    if not policy:
        raise GrowthValidationError("new_account_country_not_configured")
    return {"country": code, **deepcopy(policy)}


def audience_strategy(strategy_key: str) -> Dict[str, Any]:
    key = str(strategy_key or "BROAD").strip().upper()
    strategy = AUDIENCE_STRATEGIES.get(key)
    if not strategy:
        raise GrowthValidationError("new_account_audience_strategy_not_configured")
    return deepcopy(strategy)


def strict_meta_targeting(country: str, strategy_key: str = "BROAD") -> Dict[str, Any]:
    policy = country_audience_policy(country)
    strategy = audience_strategy(strategy_key)
    targeting: Dict[str, Any] = {
        "geo_locations": {"countries": [policy["country"]], "location_types": ["home", "recent"]},
        "genders": list(policy["genders"]),
        "age_min": int(policy["age_min"]),
        "age_max": int(policy["age_max"]),
        "locales": list(policy["locales"]),
        "app_install_state": "not_installed",
        "user_os": ["Android_ver_8.0_and_above"],
        "user_device": ["Android_Smartphone"],
        # 0 is the explicit Marketing API opt-out. Missing is not accepted.
        "targeting_automation": {"advantage_audience": 0},
    }
    targeting.update(deepcopy(strategy["detailed_targeting"]))
    return targeting


def assert_strict_targeting(
    targeting: Dict[str, Any], country: str, strategy_key: str = "BROAD",
) -> None:
    expected = strict_meta_targeting(country, strategy_key)
    actual = dict(targeting or {})
    if actual.get("genders") != expected["genders"]:
        raise GrowthValidationError("meta_gender_control_not_strict")
    if int(actual.get("age_min") or 0) != expected["age_min"] or int(actual.get("age_max") or 0) != expected["age_max"]:
        raise GrowthValidationError("meta_age_control_not_strict")
    if list(actual.get("locales") or []) != expected["locales"]:
        raise GrowthValidationError("meta_language_control_not_strict")
    if list(dict(actual.get("geo_locations") or {}).get("countries") or []) != [expected["geo_locations"]["countries"][0]]:
        raise GrowthValidationError("meta_country_control_not_strict")
    automation = dict(actual.get("targeting_automation") or {})
    if automation.get("advantage_audience") not in (0, False, "0"):
        raise GrowthValidationError("meta_advantage_audience_must_be_disabled")
    if actual.get("flexible_spec", []) != expected.get("flexible_spec", []):
        raise GrowthValidationError("meta_audience_strategy_targeting_mismatch")


def audience_contract(country: str, strategy_key: str = "BROAD") -> Dict[str, Any]:
    policy = country_audience_policy(country)
    strategy = audience_strategy(strategy_key)
    return {
        "base_conditions": {
            "country": policy["country"],
            "country_label": policy["country_label"],
            "gender": policy["gender"],
            "gender_label": "女性",
            "age_min": policy["age_min"],
            "age_max": policy["age_max"],
            "language": policy["language"],
            "language_label": policy["language_label"],
        },
        "audience_strategy": strategy,
        "advantage_audience": "DISABLED",
        "gender_as_suggestion": False,
        "age_as_suggestion": False,
    }


def ensure_audience_strategy_registry(conn: sqlite3.Connection) -> None:
    """Registry stores Meta ids, never merely a translated strategy name."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_audience_strategy_registry (
            country TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            label TEXT NOT NULL,
            meta_targeting_json TEXT NOT NULL,
            meta_targeting_ids_json TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            source_adset_id TEXT NOT NULL DEFAULT '',
            verified_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(country,strategy_key)
        )
        """
    )
    for country in COUNTRY_AUDIENCE_POLICIES:
        for strategy_key, strategy in AUDIENCE_STRATEGIES.items():
            conn.execute(
                """
                INSERT INTO ad_audience_strategy_registry
                (country,strategy_key,label,meta_targeting_json,meta_targeting_ids_json,verification_status)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(country,strategy_key) DO UPDATE SET
                  label=excluded.label,
                  meta_targeting_json=excluded.meta_targeting_json,
                  meta_targeting_ids_json=excluded.meta_targeting_ids_json,
                  verification_status=excluded.verification_status
                """,
                (
                    country, strategy_key, strategy["label"],
                    json.dumps(strategy["detailed_targeting"], separators=(",", ":"), ensure_ascii=False),
                    json.dumps(strategy["meta_targeting_ids"], separators=(",", ":")),
                    strategy["verification_status"],
                ),
            )
