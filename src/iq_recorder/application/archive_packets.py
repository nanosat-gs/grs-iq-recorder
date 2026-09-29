"""Arquivar raw packets: PacketSource -> buffer -> PacketArchive.

Em lote, e não uma transação por pacote: a ~1,5 pacote/s qualquer coisa
serviria, mas uma passagem com um beacon denso ou um replay a toda
velocidade não pode virar uma transação por pacote.

O banco pode cair no meio de uma passagem, e a passagem não espera. Então a
falha de gravação NÃO descarta o lote: ele fica no buffer e é regravado quando
o banco volta. O buffer tem teto — sem teto, um banco fora por horas viraria
um processo sem memória —, e o que passa do teto é DESCARTADO CONTANDO, com
aviso no log. Perder em silêncio é o único resultado inaceitável.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from iq_recorder.domain.ports import PacketArchive, PacketSource

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_FLUSH_SIZE = 50
DEFAULT_MAX_BUFFER = 10_000
DEFAULT_RETRY_INTERVAL_S = 5.0
DEFAULT_STATUS_INTERVAL_S = 60.0


@dataclass
class ArchiveStats:
    received: int = 0
    archived: int = 0
    dropped: int = 0
    failed_writes: int = 0
    last_error: str | None = field(default=None)


def archive_packets(
    source: PacketSource,
    archive: PacketArchive,
    cancel: threading.Event,
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    flush_size: int = DEFAULT_FLUSH_SIZE,
    max_buffer: int = DEFAULT_MAX_BUFFER,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    status_interval_s: float = DEFAULT_STATUS_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> ArchiveStats:
    stats = ArchiveStats()
    buffer: deque = deque()
    last_flush = clock()
    next_retry = 0.0
    last_status = clock()

    def flush(now: float) -> None:
        nonlocal last_flush, next_retry
        last_flush = now
        if not buffer or now < next_retry:
            return

        batch = list(buffer)
        try:
            archive.append_many(batch)
        except Exception as error:  # noqa: BLE001 — qualquer falha do banco mantém o lote
            stats.failed_writes += 1
            if stats.last_error is None:
                logger.error("Banco indisponível, %d pacote(s) no buffer; tentando de novo "
                             "a cada %.0f s. Erro: %s", len(buffer), retry_interval_s, error)
            stats.last_error = str(error)
            next_retry = now + retry_interval_s
            return

        for _ in batch:
            buffer.popleft()
        stats.archived += len(batch)
        if stats.last_error is not None:
            logger.info("Banco de volta: %d pacote(s) do buffer gravados.", len(batch))
            stats.last_error = None

    for packet in source.packets():
        now = clock()

        if packet is not None:
            stats.received += 1
            buffer.append(packet)
            if len(buffer) > max_buffer:
                buffer.popleft()
                stats.dropped += 1
                if stats.dropped == 1 or stats.dropped % 1000 == 0:
                    logger.warning("Buffer cheio (%d): %d pacote(s) mais antigo(s) "
                                   "DESCARTADO(S) sem gravar.", max_buffer, stats.dropped)

        if len(buffer) >= flush_size or now - last_flush >= flush_interval_s:
            flush(now)

        if now - last_status >= status_interval_s:
            last_status = now
            logger.info("Arquivados %d de %d recebidos; %d no buffer; %d descartados.",
                        stats.archived, stats.received, len(buffer), stats.dropped)

        if cancel.is_set():
            break

    # Última tentativa ao parar, sem esperar o intervalo de retry.
    next_retry = 0.0
    flush(clock())
    if buffer:
        stats.dropped += len(buffer)
        logger.error("Encerrando com %d pacote(s) que não puderam ser gravados.", len(buffer))

    return stats
