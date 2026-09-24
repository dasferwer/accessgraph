FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY accessgraph ./accessgraph
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "accessgraph.api:app", "--host", "0.0.0.0", "--port", "8000"]
