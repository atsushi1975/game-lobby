FROM python:3.12-slim

WORKDIR /app

COPY server.py lobby.py ./
COPY web ./web
COPY VERSION ./VERSION

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:80/health', timeout=3).status == 200 else 1)"

CMD ["python3", "server.py", "--host", "0.0.0.0", "--port", "80"]
