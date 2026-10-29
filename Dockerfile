# The pipeline image: code and libraries only. Configuration, credentials and the
# Finance workbooks are supplied by Kubernetes at run time (see k8s/pipeline.yaml).
#   docker build -t orbital-pipeline:1.0 .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY db.py ingest_usage.py ingest_commitments.py build_models.py pipeline.py ./
USER 65534:65534
ENTRYPOINT ["python", "pipeline.py"]
