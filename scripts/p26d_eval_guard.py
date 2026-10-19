"""Refuse P2.6d evaluation against the ordinary demo database."""

import os
import subprocess

from db import engine


def assert_eval_database():
    url = engine.url
    if (url.host, url.port, url.database) != ("127.0.0.1", 5434, "travel_demo"):
        raise RuntimeError("P2.6d requires the isolated database on 127.0.0.1:5434")
    if os.getenv("P26D_EVAL_PROJECT") != "travel-eval":
        raise RuntimeError("P26D_EVAL_PROJECT must identify travel-eval")
    docker = os.getenv("P26D_DOCKER_BIN", "docker")
    result = subprocess.run(
        [docker, "inspect", "--format",
         '{{index .Config.Labels "com.docker.compose.project"}}',
         "travel-eval-db-1"], capture_output=True, text=True, check=True,
        timeout=10,
    )
    if result.stdout.strip() != "travel-eval":
        raise RuntimeError("Docker container is not the isolated evaluation project")
