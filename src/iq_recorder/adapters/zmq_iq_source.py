"""Adapter de entrada: assina o PUB de IQ e entrega lotes (C1).

Implementa `IqStreamSource`. É o *tap* ao vivo: conecta no PUB que o
`grs-iq-rx` (ou o `grs-sdr-sim`) binda na :5556 e devolve exatamente os bytes
que vieram do socket, sem tocá-los.

Não tocar é a regra, não uma economia. O que for gravado a partir daqui tem de
ser byte a byte o que o rádio publicou, ou o replay republicaria um sinal que
nunca existiu — e o demodulador estaria sendo exercitado contra uma ficção.
Ver docs/capture-contract.md.

ASSINANTE, e não dono do fluxo: o PUB é de quem publica. Vários consumidores
podem grampear o mesmo tópico ao mesmo tempo — o demodulador de um lado, o
gravador do outro — e nenhum deles sabe do outro.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterator

import zmq

from iq_recorder.domain.models import IqBlock

# Sem tópico: o envelope do `grs-iq-rx` não tem frame de tópico, então a
# inscrição é vazia (tudo). Assinar uma string qualquer aqui faria o socket
# descartar todas as mensagens em silêncio.
SUBSCRIBE_ALL = ""

# Timeout do recv. Existe para o laço poder reagir a um `close()` vindo de
# outra thread: sem ele, `recv()` bloqueia para sempre e o serviço só morre
# por SIGKILL.
DEFAULT_RECEIVE_TIMEOUT_MS = 500

# Marca d'água de recepção. O IQ é o lado rápido — 1,9 MB/s a 240 kS/s — e um
# consumidor que hesita meio segundo já perde blocos. Alta de propósito:
# perder amostra no meio de uma passagem é perder a passagem.
DEFAULT_RECEIVE_HWM = 1_000_000
DEFAULT_RECEIVE_BUFFER_BYTES = 2 * 1024 * 1024


class ZmqIqSource:
    """Implementa IqStreamSource assinando um PUB de IQ."""

    def __init__(
        self,
        address: str,
        receive_timeout_ms: int = DEFAULT_RECEIVE_TIMEOUT_MS,
        context: zmq.Context | None = None,
    ) -> None:
        self.address = address

        # Context próprio quando ninguém injeta um: o mesmo motivo dos outros
        # serviços da estação — instabilidade observada no libzmq no Windows
        # com muitos sockets disputando o context global.
        self._owns_context = context is None
        self._context = context if context is not None else zmq.Context()

        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.RCVHWM, DEFAULT_RECEIVE_HWM)
        self._socket.setsockopt(zmq.RCVBUF, DEFAULT_RECEIVE_BUFFER_BYTES)
        self._socket.setsockopt(zmq.RCVTIMEO, receive_timeout_ms)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt_string(zmq.SUBSCRIBE, SUBSCRIBE_ALL)
        self._socket.connect(address)

        self._closing = threading.Event()
        self._sequence = 0

    def blocks(self) -> Iterator[IqBlock]:
        """Lotes, indefinidamente, até `close()`.

        Um timeout de recepção NÃO encerra o fluxo: entre rajadas de um
        satélite há segundos de silêncio, e desistir no primeiro silêncio
        significaria parar de gravar no meio da passagem. Quem decide quando
        parar é quem consome — por tempo, por tamanho, ou por comando.
        """
        while not self._closing.is_set():
            try:
                payload = self._socket.recv()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                # Socket fechado debaixo do laço (close() de outra thread).
                break

            # `received_at` é o relógio da ESTAÇÃO, não do satélite: marca
            # quando o byte chegou aqui, e é o que o sidecar SigMF grava como
            # início da captura.
            yield IqBlock(
                data=payload,
                sequence=self._sequence,
                received_at=datetime.now(timezone.utc),
            )
            self._sequence += 1

    def close(self) -> None:
        self._closing.set()
        self._socket.close()

        if self._owns_context:
            self._context.term()
