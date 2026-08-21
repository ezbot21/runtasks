from __future__ import annotations

import json
from typing import Mapping


def print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
