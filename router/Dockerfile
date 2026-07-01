FROM python:3.14-slim

WORKDIR /app

# Install deps in a single layer (no pip cache, no extra files)
RUN pip install --no-cache-dir fastapi "pydantic>=2.0,<3.0" uvicorn httpx pyyaml python-multipart asyncpg langfuse redis aiofiles

# Copy all source in one layer — removes dead config COPY (volume-mounted at runtime)
COPY main.py agy_proxy.py circuit_breaker.py aa_scores.json free_models_roster.json /app/
COPY static/ /app/static/

EXPOSE 5000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
