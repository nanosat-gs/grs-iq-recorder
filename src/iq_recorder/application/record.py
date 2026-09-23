"""Caso de uso: gravar uma captura (C2).

    IqStreamSource -> IqSink -> CaptureIndex

O laço é limitado de propósito. A gravação disparada por passagem (AOS/LOS)
está fora desta fatia, então toda captura é sob comando e com teto — porque a
240 kS/s são 1,92 MB/s, e "gravar a passagem inteira" sem olhar para esse
número é como se enche um disco em silêncio.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from iq_recorder.domain.models import CaptureMetadata, CaptureProfile
from iq_recorder.domain.ports import CaptureIndex, IqSink, IqStreamSource

logger = logging.getLogger(__name__)


def record(
    source: IqStreamSource,
    sink: IqSink,
    profile: CaptureProfile,
    max_seconds: float | None = None,
    max_bytes: int | None = None,
    index: CaptureIndex | None = None,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CaptureMetadata:
    """Grava até bater um limite, e devolve o que foi gravado.

    Três formas de parar, e nenhuma é opcional por acaso:

    - `max_seconds`  teto de tempo. O caso normal.
    - `max_bytes`    teto de disco. Protege contra uma taxa maior do que a
                     esperada, que nenhum teto de tempo pega.
    - `cancel`       parada externa (SIGTERM, comando). Uma passagem que
                     terminou cedo não deve obrigar ninguém a esperar o teto.

    O `finally` fecha o sink mesmo se a origem explodir no meio: uma captura
    interrompida ainda é uma captura, e o sidecar sem `core:sha512` diz
    exatamente isso a quem for lê-la depois.
    """
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError(f"max_seconds precisa ser positivo, veio {max_seconds}")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError(f"max_bytes precisa ser positivo, veio {max_bytes}")

    sink.open(profile)

    started = clock()
    written = 0
    blocks = 0

    try:
        for block in source.blocks():
            sink.write(block)
            written += len(block.data)
            blocks += 1

            if cancel is not None and cancel.is_set():
                logger.info("Gravação interrompida por comando.")
                break
            if max_bytes is not None and written >= max_bytes:
                logger.info("Teto de disco atingido: %d bytes.", written)
                break
            if max_seconds is not None and (clock() - started) >= max_seconds:
                logger.info("Teto de tempo atingido: %.1f s.", clock() - started)
                break
    finally:
        metadata = sink.close()

    logger.info(
        "Captura %s: %d lotes, %d amostras, %.1f s.",
        metadata.capture_id, blocks, metadata.sample_count, metadata.duration_seconds,
    )

    if index is not None:
        try:
            index.append(metadata)
        except Exception:
            # O índice é conveniência; a captura é o artefato. Perder a linha
            # do índice não pode custar o arquivo que já está gravado em disco.
            logger.exception("Falha ao indexar a captura %s", metadata.capture_id)

    return metadata
