"""Guard the one-off container batch commands against other databases."""

import os
import re


PUBLIC_DOMAIN = re.compile(
    r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)


def assert_container_database_url(url):
    if (not os.getenv("DATABASE_URL") or
            url.drivername != "postgresql+psycopg" or
            url.host != "db" or url.port != 5432 or
            url.database != "travel_demo" or url.username != "travel_app" or
            not url.password):
        raise RuntimeError("Container batch requires the isolated Compose database")
