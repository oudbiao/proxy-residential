ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-alpine
FROM ghcr.io/fatedier/frpc:v0.72.0 AS frpc
FROM ghcr.io/fatedier/frps:v0.72.0 AS frps
FROM ghcr.io/sagernet/sing-box:v1.14.4 AS singbox
FROM ${PYTHON_IMAGE}
RUN apk add --no-cache openssl ca-certificates && addgroup -g 10001 proxyjp && adduser -D -H -u 10001 -G proxyjp proxyjp && mkdir -p /run/proxy-jp && chown 10001:10001 /run/proxy-jp
COPY --from=frpc /usr/bin/frpc /usr/local/bin/frpc
COPY --from=frps /usr/bin/frps /usr/local/bin/frps
COPY --from=singbox /usr/local/bin/sing-box /usr/local/bin/sing-box
WORKDIR /app
COPY proxy_jp /app/proxy_jp
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=2 CMD ["python3", "-m", "proxy_jp", "health"]
ENTRYPOINT ["python3", "-m", "proxy_jp"]
CMD ["client"]
