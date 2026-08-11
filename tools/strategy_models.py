from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import json
import math


class RunStatus(str, Enum):
    OK = "ok"
    PROVISIONAL = "provisional"
    UNKNOWN = "unknown"
    NO_SIGNAL = "no_signal"


@dataclass(frozen=True)
class StrategyError:
    code: str | None
    stage: str
    source: str | None
    message: str


def to_jsonable(value: object) -> object:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def strict_json_dumps(value: object, *, indent: int | None = 2) -> str:
    return json.dumps(
        to_jsonable(value), ensure_ascii=False, indent=indent, allow_nan=False,
    )
