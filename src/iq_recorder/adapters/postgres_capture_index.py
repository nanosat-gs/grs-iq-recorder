"""Adapter de saída: índice append-only das capturas, em Postgres (C4).

Implementa `CaptureIndex`.

QUEM É O DONO DESTA TABELA. Ao contrário das tabelas que o TC Scheduler lê — que
são propriedade do TC Generator — esta é NOSSA. Isso muda o modo de criá-la, e
para melhor: como ninguém mais a define, o serviço pode criá-la no boot com
`CREATE TABLE IF NOT EXISTS`, e a armadilha do `docker-entrypoint-initdb.d`
(que só roda em volume vazio) some do caminho. Uma estação que já tem dados não
precisa de `down -v` para ganhar o índice.

Ela vive num schema próprio, `mission_control`, e não no `public` do TC
Generator. Separar é barato agora e evita que uma captura e um telecomando
disputem um nome de tabela daqui a um ano.

APPEND-ONLY, e isso é regra, não convenção: não há UPDATE nem DELETE neste
módulo. Uma captura é uma OBSERVAÇÃO — ela aconteceu, num instante, com uma
configuração. Reescrever a linha depois é reescrever o que a estação viu, e é
assim que uma regressão de DSP fica impossível de reproduzir.
"""

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from iq_recorder.domain.models import CaptureMetadata, SampleFormat

logger = logging.getLogger(__name__)

SCHEMA_NAME = "mission_control"
TABLE_NAME = "iq_captures"
QUALIFIED = f"{SCHEMA_NAME}.{TABLE_NAME}"

# As colunas que este serviço escreve e lê. O schema_check confere contra isto
# no boot — ver docs/schema-contract.md.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "capture_id",
    "profile_name",
    "center_frequency_hz",
    "sample_rate_hz",
    "datatype",
    "started_at",
    "ended_at",
    "sample_count",
    "sha512",
    "data_path",
    "indexed_at",
)

CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}"

CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {QUALIFIED} (
    capture_id          TEXT PRIMARY KEY,
    profile_name        TEXT NOT NULL,
    center_frequency_hz DOUBLE PRECISION NOT NULL,
    sample_rate_hz      DOUBLE PRECISION NOT NULL,
    datatype            TEXT NOT NULL,
    started_at          TIMESTAMP WITH TIME ZONE NOT NULL,
    ended_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    sample_count        BIGINT NOT NULL,
    -- 128 caracteres hexadecimais. É o do .sigmf-data, nunca o do sidecar: o
    -- sidecar ganha anotação depois da gravação, e hash de coisa que muda não
    -- verifica nada.
    sha512              TEXT NOT NULL,
    data_path           TEXT NOT NULL,
    indexed_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
)
"""

# Buscar por instante é a consulta natural ("o que foi gravado naquela
# passagem?"), e é a única que justifica um índice a esta altura.
CREATE_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {TABLE_NAME}_started_at_idx "
    f"ON {QUALIFIED} (started_at DESC)"
)

INSERT = f"""
INSERT INTO {QUALIFIED} (
    capture_id, profile_name, center_frequency_hz, sample_rate_hz, datatype,
    started_at, ended_at, sample_count, sha512, data_path
) VALUES (
    :capture_id, :profile_name, :center_frequency_hz, :sample_rate_hz, :datatype,
    :started_at, :ended_at, :sample_count, :sha512, :data_path
)
"""

SELECT_COLUMNS = (
    "capture_id, profile_name, center_frequency_hz, sample_rate_hz, datatype, "
    "started_at, ended_at, sample_count, sha512, data_path"
)


class PostgresCaptureIndex:
    """Implementa CaptureIndex sobre Postgres."""

    def __init__(self, database_url: str, engine: Engine | None = None) -> None:
        self._engine = engine if engine is not None else create_engine(
            database_url, pool_pre_ping=True
        )

    def ensure_schema(self) -> None:
        """Cria schema, tabela e índice se não existirem.

        Idempotente de propósito: roda a cada boot. Como a tabela é nossa, não
        há dono externo a consultar nem migration a coordenar.
        """
        with self._engine.begin() as connection:
            connection.execute(text(CREATE_SCHEMA))
            connection.execute(text(CREATE_TABLE))
            connection.execute(text(CREATE_INDEX))

        logger.info("Índice de capturas pronto em %s", QUALIFIED)

    # --- CaptureIndex -------------------------------------------------------

    def append(self, metadata: CaptureMetadata) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(INSERT), _to_row(metadata))

    def list_captures(self, limit: int = 100) -> Iterable[CaptureMetadata]:
        if limit <= 0:
            raise ValueError(f"limit precisa ser positivo, veio {limit}")

        query = text(
            f"SELECT {SELECT_COLUMNS} FROM {QUALIFIED} ORDER BY started_at DESC LIMIT :limit"
        )
        with self._engine.connect() as connection:
            return [_from_row(row) for row in connection.execute(query, {"limit": limit})]

    def get(self, capture_id: str) -> CaptureMetadata | None:
        query = text(f"SELECT {SELECT_COLUMNS} FROM {QUALIFIED} WHERE capture_id = :capture_id")

        with self._engine.connect() as connection:
            row = connection.execute(query, {"capture_id": capture_id}).first()

        return _from_row(row) if row is not None else None

    def close(self) -> None:
        self._engine.dispose()


def _to_row(metadata: CaptureMetadata) -> dict:
    return {
        "capture_id": metadata.capture_id,
        "profile_name": metadata.profile_name,
        "center_frequency_hz": metadata.center_frequency_hz,
        "sample_rate_hz": metadata.sample_rate_hz,
        "datatype": metadata.datatype.value,
        "started_at": metadata.started_at,
        "ended_at": metadata.ended_at,
        "sample_count": metadata.sample_count,
        "sha512": metadata.sha512,
        "data_path": metadata.data_path,
    }


def _from_row(row) -> CaptureMetadata:
    return CaptureMetadata(
        capture_id=row.capture_id,
        profile_name=row.profile_name,
        center_frequency_hz=float(row.center_frequency_hz),
        sample_rate_hz=float(row.sample_rate_hz),
        datatype=SampleFormat(row.datatype),
        started_at=row.started_at,
        ended_at=row.ended_at,
        sample_count=int(row.sample_count),
        sha512=row.sha512,
        data_path=row.data_path,
    )
