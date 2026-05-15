FROM alpine:3.21

RUN apk add --no-cache python3 nvme-cli docker-cli

WORKDIR /app
COPY dashboard.py .

EXPOSE 3999
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:3999/ || exit 1

CMD ["python3", "dashboard.py"]
