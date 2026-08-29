FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY acm ./acm
RUN pip install --no-cache-dir ".[dev]"
EXPOSE 8927
CMD ["uvicorn", "acm.api:app", "--host", "0.0.0.0", "--port", "8927"]
