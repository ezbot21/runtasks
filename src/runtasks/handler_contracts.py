from __future__ import annotations

from typing import Mapping


HANDLER_ACTION_MODES: Mapping[str, frozenset[str]] = {
    "manual_notification": frozenset({"notify"}),
    "pi_mcp_adapter": frozenset({"check", "approved-procedure"}),
}
