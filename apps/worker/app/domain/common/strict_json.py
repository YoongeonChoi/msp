from __future__ import annotations

import json
from collections.abc import Sequence


class StrictJsonError(ValueError):
    pass


def loads_strict_json(payload: str) -> object:
    return json.loads(
        payload,
        object_pairs_hook=_reject_duplicate_object_names,
        parse_constant=_reject_non_standard_constant,
    )


def _reject_duplicate_object_names(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate_json_object_name")
        result[key] = value
    return result


def _reject_non_standard_constant(value: str) -> object:
    raise StrictJsonError(f"non_standard_json_constant_{value.casefold()}")
