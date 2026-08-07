from __future__ import annotations

from typing import Optional


OPS_AUTH_SESSION_COOKIE = 'mcn_ops_session'
OPS_AUTH_INTERNAL_HEADER = 'x-ops-internal-token'
OPS_AUTH_ROLE_SUPER_ADMIN = 'super_admin'
OPS_AUTH_ROLE_ADMIN = 'admin'
OPS_AUTH_ROLE_CUSTOMER_SERVICE = 'customer_service'
OPS_AUTH_ROLE_OPERATOR = 'operator'
OPS_AUTH_ROLE_INTERNAL = 'internal'
OPS_AUTH_ALLOWED_ROLES = {
    OPS_AUTH_ROLE_SUPER_ADMIN,
    OPS_AUTH_ROLE_ADMIN,
    OPS_AUTH_ROLE_CUSTOMER_SERVICE,
    OPS_AUTH_ROLE_OPERATOR,
}
OPS_AUTH_BUSINESS_ROLES = {
    OPS_AUTH_ROLE_CUSTOMER_SERVICE,
    OPS_AUTH_ROLE_OPERATOR,
}


def normalize_ops_role(value: Optional[str]) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in OPS_AUTH_ALLOWED_ROLES else OPS_AUTH_ROLE_OPERATOR


def ops_role_is_business(value: Optional[str]) -> bool:
    return str(value or '').strip().lower() in OPS_AUTH_BUSINESS_ROLES
