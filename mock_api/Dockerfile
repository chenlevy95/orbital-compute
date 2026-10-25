# Mock Orbital Compute Billing API. Provided as-is: build it, don't change it.
#   docker build -t orbital-billing-api:1.0 mock_api
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8765
WORKDIR /app
COPY server.py usage_versions.jsonl.gz ./
USER 65534:65534
EXPOSE 8765
ENTRYPOINT ["python", "server.py"]
