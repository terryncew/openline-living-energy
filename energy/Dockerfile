# Optional background operation on an existing local host. Image pull is not offline.
FROM python:3.12-alpine
WORKDIR /app
COPY src /app/src
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
CMD ["python3","-m","autopilot","loop","--config","/data/config.json","--state","/data/private/state.json","--token-file","/data/ha-token.txt","--interval","60","--live"]
