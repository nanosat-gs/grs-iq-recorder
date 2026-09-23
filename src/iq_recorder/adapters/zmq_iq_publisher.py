"""Adapter de saída: republica IQ no mesmo tópico do rádio (C3).

Implementa `IqStreamPublisher`. É a outra metade do replay: com o
`grs-iq-rx` (ou o `grs-sdr-sim`) DESLIGADO, este publicador ocupa a :5556 e o
resto do cano não tem como saber que do outro lado há um arquivo.

BIND, e não connect — e é essa a armadilha a conhecer: no replay o gravador
toma o lugar do publicador de IQ. Os dois juntos disputam a porta, e quem
perde cai em silêncio.
"""

from __future__ import annotations

import time

import zmq

from iq_recorder.domain.models import IqBlock

# PUB descarta o que publica enquanto nenhum assinante concluiu a conexão. Sem
# esta pausa, os primeiros blocos do replay somem e alguém passa a tarde
# procurando o defeito no demodulador.
DEFAULT_SETTLE_SECONDS = 1.0

# Tempo para o socket drenar no close(). Sem ele o último bloco do replay
# morre com o processo, e uma captura curta pode perder o único frame que
# tinha.
DEFAULT_LINGER_MS = 1000


class ZmqIqPublisher:
    """Implementa IqStreamPublisher publicando lotes num PUB."""

    def __init__(
        self,
        bind_address: str,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        context: zmq.Context | None = None,
    ) -> None:
        self.bind_address = bind_address

        self._owns_context = context is None
        self._context = context if context is not None else zmq.Context()

        self._socket = self._context.socket(zmq.PUB)
        self._socket.setsockopt(zmq.LINGER, DEFAULT_LINGER_MS)
        self._socket.bind(bind_address)

        self._published = 0

        if settle_seconds > 0:
            time.sleep(settle_seconds)

    @property
    def published_blocks(self) -> int:
        return self._published

    def publish(self, block: IqBlock) -> None:
        """Um lote, uma mensagem, SEM frame de tópico.

        Sem tópico porque é assim que o `grs-iq-rx` publica, e o demodulador
        faz um `recv()` simples — um frame de tópico na frente viraria a
        primeira mensagem dele. Ser indistinguível do vivo é o requisito, não
        uma coincidência.
        """
        self._socket.send(block.data)
        self._published += 1

    def close(self) -> None:
        self._socket.close()

        if self._owns_context:
            self._context.term()
