"""Report credential-shaped Git exposures without printing credential values."""

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(rb"(?:sk-|tvly-)[A-Za-z0-9_-]{20,}")
HISTORICAL_PATHS = (
    "graph_chat/llm_tavily.py",
    "tools/retriever_vector.py",
    "config/development.yml",
)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          check=False).stdout


def main():
    tracked = git("ls-files", "-z").split(b"\0")
    current = []
    for raw in tracked:
        if not raw:
            continue
        path = ROOT / raw.decode("utf-8", errors="surrogateescape")
        if path.is_file() and PATTERN.search(path.read_bytes()):
            current.append(path.relative_to(ROOT).as_posix())
    history = {}
    for path in HISTORICAL_PATHS:
        commits = git("log", "--all", "--format=%H", "--", path).splitlines()
        matched = [sha.decode("ascii")[:12] for sha in commits
                   if PATTERN.search(git("show", sha.decode("ascii") + ":" + path))]
        history[path] = {"revisions_checked": len(commits),
                         "matching_revision_count": len(matched),
                         "matching_revision_ids": matched[:5]}
    print({"current_credential_shape_files": current,
           "historical_exposure": history,
           "env_tracked": bool(git("ls-files", "--error-unmatch", ".env"))})


if __name__ == "__main__":
    main()
