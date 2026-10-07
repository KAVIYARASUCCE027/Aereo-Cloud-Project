# One image for the API, the worker, and migrations; Compose picks the command.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

# The certificate volume is mounted here. Creating it in the image gives a
# fresh named volume the right owner, so the non-root user can write to it.
RUN mkdir -p /data/certificates && chown -R app:app /data

USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Runtime image plus test dependencies and the test suite.
FROM runtime AS dev

USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY pyproject.toml .
COPY tests ./tests
COPY scripts ./scripts
# Integration tests submit the documented sample requests.
COPY examples ./examples
USER app

# /app is not writable by the app user, and the cache has no value in a throwaway container.
ENV PYTEST_ADDOPTS="-p no:cacheprovider"

CMD ["pytest"]
