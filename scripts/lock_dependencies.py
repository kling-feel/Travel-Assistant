"""Pin the installed runtime dependency closure for Linux Python 3.11 builds."""

from collections import deque
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "requirements.txt"
TARGET = ROOT / "requirements.lock"
LINUX = {**default_environment(), "sys_platform": "linux", "platform_system": "Linux",
         "platform_machine": "x86_64", "python_version": "3.11", "extra": ""}


def main():
    selected = {}
    pending = deque(Requirement(line) for line in SOURCE.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#"))
    while pending:
        requirement = pending.popleft()
        name = canonicalize_name(requirement.name)
        extras = set(requirement.extras)
        known = selected.get(name)
        if known is not None and extras <= known:
            continue
        if known is None:
            known = set()
            selected[name] = known
        known.update(extras)
        distribution = metadata.distribution(name)
        if distribution.version not in requirement.specifier:
            raise RuntimeError(f"Installed version does not satisfy {requirement.name}")
        for raw in distribution.requires or ():
            child = Requirement(raw)
            if child.marker is None or any(child.marker.evaluate({**LINUX, "extra": extra})
                                            for extra in ("", *known)):
                pending.append(child)
    lines = [f"{name}=={metadata.version(name)}" for name in sorted(selected)]
    TARGET.write_text("# Generated from requirements.txt for Linux x86_64 / Python 3.11.\n"
                      + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"Pinned {len(lines)} runtime distributions")


if __name__ == "__main__":
    main()
