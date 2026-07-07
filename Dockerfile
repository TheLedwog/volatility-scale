# Backend image for the product deploy (Railway / Render / Fly).
# The frontend (Next.js) is a separate repo on Vercel and is NOT built here.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# scikit-learn / scipy / pandas / numpy all ship manylinux wheels for cp311, so no
# compiler is needed. If a future dep needs building, add build-essential here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The host injects PORT; run.py reads HOST/PORT from the environment.
EXPOSE 8000
CMD ["python", "run.py"]
