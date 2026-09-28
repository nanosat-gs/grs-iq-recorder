"""Adapter de saída: republica IQ no mesmo tópico do rádio (C3).

Implementa `IqStreamPublisher`. É a outra metade do replay: com o
`grs-iq-rx` (ou o `grs-sdr-sim`) DESLIGADO, este publicador ocupa a :5556 e o
resto do cano não tem como saber que do outro lado há um arquivo.

BIND, e não connect — e é essa a armadilha a conhecer: no replay o gravador
toma o lugar do publicador de IQ. Os dois juntos disputam a porta, e quem
perde cai em silêncio.
"""

from __future__ import annotations

import logging
import time

import zmq

from iq_recorder.domain.models import IqBlock

logger = logging.getLogger(__name__)

# PUB descarta o que publica enquanto nenhum assinante concluiu a conexão. Sem
# esta pausa, os primeiros blocos do replay somem e alguém passa a tarde
# procurando o defeito no demodulador.
DEFAULT_SETTLE_SECONDS = 1.0

# Uma pausa FIXA não basta no compose: com a fonte ao vivo desligada, o
# demodulador fica tentando resolver um nome que não existe, e a consulta de
# DNS trava a thread de I/O do ZMQ por segundos. Medido: dois replays do mesmo
# arquivo deram 13 e 9 pacotes, de ~16. Com `wait_for_subscriber_s`, o socket
# vira XPUB e o replay só começa quando alguém de fato se inscreveu.
DEFAULT_SUBSCRIBER_TIMEOUT_S = 20.0
POST_SUBSCRIBE_SETTLE_S = 0.2

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
        wait_for_subscriber_s: float = 0.0,
    ) -> None:
        self.bind_address = bind_address

        self._owns_context = context is None
        self._context = context if context is not None else zmq.Context()

        # XPUB publica exatamente como PUB (mesmo envelope, mesmo filtro), e
        # além disso entrega as inscrições — que é como se sabe que o
        # demodulador está ouvindo.
        self._socket = self._context.socket(zmq.XPUB if wait_for_subscriber_s > 0 else zmq.PUB)
        self._socket.setsockopt(zmq.LINGER, DEFAULT_LINGER_MS)
        self._socket.bind(bind_address)

        self._published = 0
        self.subscribed = False

        if wait_for_subscriber_s > 0:
            self.subscribed = self._wait_subscription(wait_for_subscriber_s)
        elif settle_seconds > 0:
            time.sleep(settle_seconds)

    def _wait_subscription(self, timeout_s: float) -> bool:
        logger.info("Esperando um assinante em %s (até %.0f s)…", self.bind_address, timeout_s)
        if not self._socket.poll(int(timeout_s * 1000), zmq.POLLIN):
            logger.warning("Ninguém se inscreveu em %.0f s — publicando assim mesmo. O "
                           "demodulador está de pé e apontado para esta fonte?", timeout_s)
            return False
        self._socket.recv()  # a inscrição: \x01 + tópico
        time.sleep(POST_SUBSCRIBE_SETTLE_S)
        logger.info("Assinante conectado; começando.")
        return True

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
