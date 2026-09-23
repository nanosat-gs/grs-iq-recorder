"""Caso de uso: reproduzir uma captura (C3).

    IqStreamSource -> IqStreamPublisher

Repare que a porta da ESQUERDA é a mesma de `record`. Gravar de um rádio e
reproduzir de um arquivo são o mesmo laço com adapters diferentes — é essa
simetria que faz o teste ponta a ponta rodar sem hardware, e é o motivo de o
serviço ter sido desenhado em portas desde o esqueleto.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from iq_recorder.domain.models import CaptureProfile
from iq_recorder.domain.ports import IqStreamPublisher, IqStreamSource

logger = logging.getLogger(__name__)


def replay(
    source: IqStreamSource,
    publisher: IqStreamPublisher,
    profile: CaptureProfile,
    realtime: bool = True,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Republica a captura. Devolve quantos lotes saíram.

    `realtime` ritma a saída pela taxa de amostragem do perfil, em vez de
    despejar o arquivo o mais rápido possível. Ligado por padrão, e por um
    motivo concreto: o demodulador tem marca d'água de recepção finita, e um
    despejo a toda velocidade estoura a fila e o ZMQ começa a DESCARTAR. O
    resultado é um replay que perde blocos — e a perda aparece como falha de
    demodulação, que é o sintoma mais caro de diagnosticar.

    Desligue para testes offline, onde do outro lado não há socket nenhum.
    """
    bytes_per_second = profile.bytes_per_second
    started = clock()
    sent_bytes = 0
    blocks = 0

    try:
        for block in source.blocks():
            if cancel is not None and cancel.is_set():
                logger.info("Replay interrompido por comando.")
                break

            publisher.publish(block)
            sent_bytes += len(block.data)
            blocks += 1

            if realtime and bytes_per_second > 0:
                # Ritmo pelo total ACUMULADO, não por um sleep fixo por bloco:
                # com sleep fixo o erro de cada iteração se soma e o replay
                # deriva do tempo real ao longo de uma captura inteira.
                target = started + (sent_bytes / bytes_per_second)
                slack = target - clock()
                if slack > 0:
                    sleep(slack)
    finally:
        source.close()

    logger.info(
        "Replay de %s: %d lotes, %d bytes, %.1f s.",
        profile.name, blocks, sent_bytes, clock() - started,
    )

    return blocks
