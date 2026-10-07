# Playwright's image ships Python 3.12 and a matching headless Chromium, which renders the posts.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .
EXPOSE 8000
CMD ["uvicorn", "studio.main:app", "--host", "0.0.0.0", "--port", "8000"]
