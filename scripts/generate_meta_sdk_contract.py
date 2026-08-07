from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.meta_sdk_contract import (
    META_GRAPH_API_VERSION,
    META_SDK_CONTRACT_VERSION,
    META_SDK_PACKAGE_VERSION,
    SDK_OBJECT_CONTRACT,
    contract_hash,
)


def _public_values(owner: Any) -> Dict[str, Any]:
    return {
        name: value for name, value in vars(owner).items()
        if not name.startswith("_") and isinstance(value, (str, int, float, bool))
    }


def build_manifest() -> Dict[str, Any]:
    from facebook_business import apiconfig

    sdk_config = dict(apiconfig.ads_api_config or {})
    installed_sdk = str(sdk_config.get("SDK_VERSION") or "").lstrip("v")
    installed_api = str(sdk_config.get("API_VERSION") or "")
    if installed_sdk != META_SDK_PACKAGE_VERSION or installed_api != META_GRAPH_API_VERSION:
        raise RuntimeError(
            f"meta_sdk_version_mismatch:expected={META_SDK_PACKAGE_VERSION}/{META_GRAPH_API_VERSION}:"
            f"actual={installed_sdk}/{installed_api}"
        )
    objects: Dict[str, Any] = {}
    for object_name, contract in SDK_OBJECT_CONTRACT.items():
        module = importlib.import_module(str(contract["module"]))
        object_class = getattr(module, str(contract["class"]))
        available_fields = set(_public_values(object_class.Field).values())
        required_fields = set(contract["read_fields"]) | set(contract["write_fields"])
        missing_fields = sorted(required_fields - available_fields)
        missing_methods = sorted(method for method in contract["methods"] if not hasattr(object_class, method))
        if missing_fields or missing_methods:
            raise RuntimeError(
                f"meta_sdk_contract_missing:{object_name}:fields={missing_fields}:methods={missing_methods}"
            )
        enum_values = {}
        for enum_name in contract["enums"]:
            enum_owner = getattr(object_class, enum_name, None)
            if enum_owner is None:
                raise RuntimeError(f"meta_sdk_enum_missing:{object_name}:{enum_name}")
            enum_values[enum_name] = _public_values(enum_owner)
        objects[object_name] = {
            "module": contract["module"],
            "read_fields": sorted(contract["read_fields"]),
            "write_fields": sorted(contract["write_fields"]),
            "methods": sorted(contract["methods"]),
            "enum_values": enum_values,
        }
    manifest = {
        "contract_version": META_SDK_CONTRACT_VERSION,
        "sdk_package": "facebook-business",
        "sdk_version": META_SDK_PACKAGE_VERSION,
        "graph_api_version": META_GRAPH_API_VERSION,
        "objects": objects,
    }
    manifest["contract_hash"] = contract_hash(manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate/check the GLE Meta SDK allow-contract.")
    parser.add_argument("--output", default="config/meta_sdk_contract_v25_0_1.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    rendered = json.dumps(build_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("meta_sdk_contract_drift")
        print("meta_sdk_contract=unchanged")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
