ARG PYTHON_BASE_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_BASE_IMAGE} AS runtime
ARG REQUIREMENTS_FILE=requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r "$REQUIREMENTS_FILE" \
    && groupadd --gid 10001 travel \
    && useradd --uid 10001 --gid travel --no-create-home travel

COPY --chown=travel:travel main.py .
COPY --chown=travel:travel travel_assistant/ travel_assistant/
COPY --chown=travel:travel migrations/ migrations/
COPY --chown=travel:travel scripts/ scripts/
COPY --chown=travel:travel data/fixtures/recommendation_geography.json data/fixtures/
COPY --chown=travel:travel data/fixtures/reviewed_places_v1.psv data/fixtures/
COPY --chown=travel:travel data/fixtures/reviewed_places_v2.psv data/fixtures/
COPY --chown=travel:travel data/fixtures/reviewed_place_display_v1.psv data/fixtures/
COPY --chown=travel:travel data/fixtures/network_source.sqlite data/fixtures/

USER travel
EXPOSE 8000
CMD ["python", "scripts/run_container.py"]

FROM runtime AS initializer
USER root
CMD ["python", "scripts/initialize_container_batch.py"]
