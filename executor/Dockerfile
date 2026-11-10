# Build context is this directory only; the image gets executor.py and owner_bot.py, nothing else.
FROM python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
RUN useradd --system --uid 10001 --no-create-home executor && install -d -o executor /data
COPY executor.py owner_bot.py /app/
USER executor
ENTRYPOINT ["python", "-I", "/app/executor.py"]
CMD ["serve"]
