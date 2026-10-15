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
CMD ["python3", "pipeline.py"]
