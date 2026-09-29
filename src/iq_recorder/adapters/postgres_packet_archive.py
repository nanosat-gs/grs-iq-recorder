"""Adapter de saída: arquivo append-only de raw packets, em Postgres.

Implementa `PacketArchive`. Mesma disciplina do índice de capturas: a tabela
é NOSSA, vive no schema `mission_control`, nasce no boot com
`CREATE TABLE IF NOT EXISTS`, e não há UPDATE nem DELETE neste módulo.

Um raw packet é uma observação: o detector achou um syncword, num instante,
e publicou os bytes seguintes. Reescrever a linha depois é reescrever o que a
estação recebeu.

O que esta tabela NÃO é: telemetria. Os bytes são os 255 depois do syncword,
sem saber onde o quadro NGHam termina; a decodificação é a próxima fatia, e
ela lê daqui — é por isso que o cru é guardado inteiro.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from iq_recorder.domain.models import RawPacket

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA = "mission_control"
TABLE_NAME = "raw_packets"

# As colunas que este serviço escreve e lê. O schema_check confere no boot.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "id",
    "received_at",
    "detected_at",
    "detector_seq",
    "bit_offset",
    "syncword",
    "max_sync_errors",
    "payload",
    "payload_sha256",
    "header",
    "archiver_run",
    "archived_at",
)


def _ddl(schema: str) -> list[str]:
    qualified = f"{schema}.{TABLE_NAME}"
    return [
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        f"""
        CREATE TABLE IF NOT EXISTS {qualified} (
            id               BIGSERIAL PRIMARY KEY,
            -- Relógio da estação, ao chegar ao arquivador (microssegundos).
            received_at      TIMESTAMP WITH TIME ZONE NOT NULL,
            -- Relógio do detector, resolução de 1 s.
            detected_at      TIMESTAMP WITH TIME ZONE,
            -- seq e bit_offset recomeçam quando o detector reinicia; só são
            -- únicos dentro de uma sessão do detector.
            detector_seq     BIGINT,
            bit_offset       BIGINT,
            syncword         TEXT,
            -- A TOLERÂNCIA em vigor, não a distância medida: o detector não a
            -- conhece. Ver o cabeçalho do service.c.
            max_sync_errors  INTEGER,
            payload          BYTEA NOT NULL,
            -- Para agrupar e achar repetidos. NÃO é chave única: o mesmo
            -- payload chegando duas vezes são duas recepções, e o replay de
            -- uma captura gera cópias legítimas dos pacotes do vivo.
            payload_sha256   TEXT NOT NULL,
            header           JSONB NOT NULL,
            -- Uma por processo do arquivador: separa sessões, já que a
            -- numeração do detector não é única entre reinícios.
            archiver_run     UUID NOT NULL,
            archived_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
        """,
        f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_received_at_idx "
        f"ON {qualified} (received_at DESC)",
        f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_payload_sha256_idx "
        f"ON {qualified} (payload_sha256)",
    ]


class PostgresPacketArchive:
    """Implementa PacketArchive sobre Postgres."""

    def __init__(self, database_url: str, schema: str = DEFAULT_SCHEMA,
                 engine: Engine | None = None, run_id: uuid.UUID | None = None) -> None:
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"nome de schema inválido: {schema!r}")

        self.schema = schema
        self.qualified = f"{schema}.{TABLE_NAME}"
        self.run_id = run_id or uuid.uuid4()
        # connect_timeout curto: com o banco fora, o padrão da libpq deixava
        # cada tentativa pendurada por dezenas de segundos, e o laço inteiro
        # esperava junto — medido: ~30 s até voltar a gravar depois de o
        # Postgres já estar de pé. Nada se perdia (o ZMQ enfileira), mas o
        # serviço ficava surdo a SIGTERM nesse tempo.
        self._engine = engine if engine is not None else create_engine(
            database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
        )
        self._insert = text(f"""
            INSERT INTO {self.qualified} (
                received_at, detected_at, detector_seq, bit_offset, syncword,
                max_sync_errors, payload, payload_sha256, header, archiver_run
            ) VALUES (
                :received_at, :detected_at, :detector_seq, :bit_offset, :syncword,
                :max_sync_errors, :payload, :payload_sha256, CAST(:header AS JSONB),
                :archiver_run
            )
        """)

    @property
    def engine(self) -> Engine:
        return self._engine

    def ensure_schema(self) -> None:
        with self._engine.begin() as connection:
            for statement in _ddl(self.schema):
                connection.execute(text(statement))

        logger.info("Arquivo de raw packets pronto em %s", self.qualified)

    # --- PacketArchive --------------------------------------------------------

    def append_many(self, packets: list[RawPacket]) -> None:
        """Tudo ou nada, numa transação: um lote gravado pela metade deixaria
        a aplicação sem saber o que regravar."""
        if not packets:
            return

        rows = [self._row(packet) for packet in packets]
        with self._engine.begin() as connection:
            connection.execute(self._insert, rows)

    def recent(self, limit: int = 20) -> list[dict]:
        if limit <= 0:
            raise ValueError(f"limit precisa ser positivo, veio {limit}")

        query = text(f"""
            SELECT id, received_at, detected_at, detector_seq, bit_offset, syncword,
                   max_sync_errors, length(payload) AS bytes, payload_sha256,
                   substring(payload from 1 for 16) AS head, archiver_run
            FROM {self.qualified}
            ORDER BY received_at DESC, id DESC
            LIMIT :limit
        """)
        with self._engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(query, {"limit": limit})]

    def count(self) -> int:
        with self._engine.connect() as connection:
            return int(connection.execute(text(f"SELECT count(*) FROM {self.qualified}")).scalar())

    def close(self) -> None:
        self._engine.dispose()

    def _row(self, packet: RawPacket) -> dict:
        return {
            "received_at": packet.received_at,
            "detected_at": packet.detected_at,
            "detector_seq": packet.detector_seq,
            "bit_offset": packet.bit_offset,
            "syncword": packet.syncword,
            "max_sync_errors": packet.max_sync_errors,
            "payload": packet.payload,
            "payload_sha256": hashlib.sha256(packet.payload).hexdigest(),
            "header": json.dumps(packet.header, sort_keys=True),
            "archiver_run": str(self.run_id),
        }
