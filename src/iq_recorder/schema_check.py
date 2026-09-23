"""Confere, no boot, se o índice tem as colunas que este serviço usa.

Mesma disciplina do TC Scheduler, por um motivo um pouco diferente. Lá o schema
é de OUTRO repositório (o TC Generator) e a divergência vem de alguém mudar
uma coluna sem saber quem a lê. Aqui a tabela é nossa e nasce de
`ensure_schema()`, então a divergência só aparece de dois jeitos:

- alguém alterou a tabela à mão no banco, e o serviço passa a escrever numa
  coluna que sumiu;
- uma versão mais nova do serviço subiu contra um banco criado por uma antiga,
  que não tinha as colunas novas — `CREATE TABLE IF NOT EXISTS` não adiciona
  coluna a uma tabela que já existe.

O segundo caso é o silencioso e o mais provável. Sem esta conferência ele
aparece como um erro de SQL no fim de uma gravação — depois de a passagem ter
acontecido e o arquivo já estar em disco, quando não há mais o que fazer.

LOGA E SEGUE, nunca levanta. A captura é o artefato; o índice é conveniência.
Derrubar o gravador por causa de uma coluna faltando trocaria um problema
pequeno (uma linha de índice que não entra) por um grande (a passagem inteira
perdida).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

from iq_recorder.adapters.postgres_capture_index import (
    QUALIFIED,
    REQUIRED_COLUMNS,
    SCHEMA_NAME,
    TABLE_NAME,
)

logger = logging.getLogger(__name__)

QUERY = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = :schema AND table_name = :table"
)


def check_schema(engine: Engine) -> bool:
    """:return: True se o índice está como este serviço espera."""
    try:
        with engine.connect() as connection:
            found = {row.column_name for row in connection.execute(
                QUERY, {"schema": SCHEMA_NAME, "table": TABLE_NAME}
            )}
    except Exception:
        logger.exception(
            "Não consegui conferir o índice de capturas. A gravação segue; "
            "a indexação pode falhar no fim de cada captura."
        )
        return False

    if not found:
        logger.warning(
            "Tabela %s não existe. O ensure_schema() do boot deveria tê-la criado — "
            "confira as permissões do usuário do banco para CREATE SCHEMA.",
            QUALIFIED,
        )
        return False

    missing = [column for column in REQUIRED_COLUMNS if column not in found]

    if missing:
        logger.warning(
            "Índice de capturas divergente: %s não tem %s. Uma tabela criada por "
            "uma versão anterior não ganha colunas novas no CREATE TABLE IF NOT "
            "EXISTS — é preciso ALTER TABLE à mão.",
            QUALIFIED, ", ".join(missing),
        )
        return False

    logger.info("Índice de capturas confere: %d colunas em %s.", len(REQUIRED_COLUMNS), QUALIFIED)

    return True
