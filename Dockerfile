FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-autonomous.txt requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-service.txt \
    && groupadd --gid 10001 baseus \
    && useradd --uid 10001 --gid 10001 --create-home baseus \
    && mkdir -p /data /archive && chown baseus:baseus /data /archive
COPY baseus_s1 ./baseus_s1
COPY baseus_autonomous.py baseus_service.py ./
USER baseus
VOLUME ["/data"]
EXPOSE 8099
CMD ["python", "baseus_service.py", "serve"]
