FROM python:3.11-slim

RUN pip install --no-cache-dir kopf==1.44.6 kubernetes==36.0.3 \
 && useradd --uid 10001 --no-create-home kopf

WORKDIR /app
COPY controller.py /app/controller.py

USER 10001
ENTRYPOINT ["kopf", "run", "--standalone", "--liveness=http://0.0.0.0:8080/healthz", "/app/controller.py"]
