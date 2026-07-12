import pytest

from app.domain.common.strict_json import StrictJsonError, loads_strict_json


def test_strict_json_accepts_unique_standard_json() -> None:
    assert loads_strict_json('{"value": 1, "nested": {"safe": true}}') == {
        "value": 1,
        "nested": {"safe": True},
    }


@pytest.mark.parametrize(
    "payload",
    [
        '{"value": 1, "value": 2}',
        '{"nested": {"value": 1, "value": 2}}',
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": -Infinity}',
    ],
)
def test_strict_json_rejects_ambiguous_or_non_standard_values(payload: str) -> None:
    with pytest.raises(StrictJsonError):
        loads_strict_json(payload)
