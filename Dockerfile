# GRS IQ Recorder — captura, replay e índice do fluxo de IQ.
#
# Ao contrário dos três blocos de RF adotados, este é NOSSO repositório, então
# o Dockerfile mora aqui dentro, como nos outros blocos da estação.
#
# Sem gcc e sem libpq-dev: numpy e psycopg2-BINARY vêm de wheel manylinux no
# CPython 3.11. O sufixo -binary no psycopg2 é o que evita arrastar toolchain
# de compilação e headers do Postgres para dentro da imagem.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# As capturas são grandes (a 240 kS/s, 1,9 MB/s) e têm de sobreviver ao
# container. No compose isto é um volume nomeado; aqui o diretório existe para
# que o serviço suba mesmo sem volume montado, em vez de morrer no primeiro
# open().
RUN mkdir -p /app/captures
VOLUME ["/app/captures"]

CMD ["python", "-m", "iq_recorder.main"]
