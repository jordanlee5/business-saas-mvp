FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-dev.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt \
    && python -c "from rapidocr import RapidOCR; RapidOCR()"
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY tools ./tools
RUN groupadd --gid 10001 saas && useradd --uid 10001 --gid saas --no-create-home saas \
    && mkdir -p /data/uploads && chown -R saas:saas /data \
    && ln -s /data/uploads /app/uploads
USER 10001:10001
CMD ["python", "-m", "tools.staging_server", "serve"]
