from __future__ import annotations


ACTIVE_CHECKPOINT_DAYS = {"D1": 1, "D3": 3, "D5": 5}
ACTIVE_CHECKPOINTS = tuple(ACTIVE_CHECKPOINT_DAYS)
FINAL_CHECKPOINT = "D5"

# D7 remains a storage/read compatibility value for experiments that closed
# before the D1/D3/D5 contract was introduced. New evaluations must never
# create it.
LEGACY_CHECKPOINTS = ("D7",)
STORAGE_CHECKPOINTS = ACTIVE_CHECKPOINTS + LEGACY_CHECKPOINTS
CHECKPOINT_ORDER = {"D0": 0, **ACTIVE_CHECKPOINT_DAYS, "D7": 7}
TERMINAL_CHECKPOINTS = frozenset({FINAL_CHECKPOINT, *LEGACY_CHECKPOINTS})


def checkpoint_sort_key(value: object) -> int:
    return CHECKPOINT_ORDER.get(str(value or "").upper(), 999)


def is_terminal_checkpoint(value: object) -> bool:
    return str(value or "").upper() in TERMINAL_CHECKPOINTS
