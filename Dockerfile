FROM eclipse-temurin:21-jre-jammy

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip unzip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app/ .

ENV WORK_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
