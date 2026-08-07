class GrowthError(RuntimeError):
    code = "GROWTH_ERROR"
    http_status = 400


class GrowthNotFound(GrowthError):
    code = "NOT_FOUND"
    http_status = 404


class GrowthStateConflict(GrowthError):
    code = "STATE_CONFLICT"
    http_status = 409


class GrowthValidationError(GrowthError):
    code = "INVALID_REQUEST"
    http_status = 400


class GrowthLegacyReadOnly(GrowthError):
    code = "LEGACY_DATA_READ_ONLY"
    http_status = 409
