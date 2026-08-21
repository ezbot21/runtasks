from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "bin" / "runtasks"


def run_cli(
    home: Path | None,
    *arguments: str,
    extra_environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RUNTASKS_")
    }
    if home is not None:
        environment["HOME"] = str(home.parent)
        environment["RUNTASKS_HOME"] = str(home)
    if extra_environment is not None:
        environment.update(extra_environment)
    return subprocess.run(
        [str(CLI), *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
