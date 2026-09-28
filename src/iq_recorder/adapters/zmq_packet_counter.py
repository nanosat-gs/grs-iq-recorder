"""Conta os raw packets que o detector de syncword publica durante uma
gravação ou um replay.

É a metade "contagem de frames" da leitura mínima (D2): o PSD diz que há
sinal; isto diz que o cano transformou esse sinal em pacotes. O gravador não
demodula nada — o DSP é GPL e roda em processo próprio —, então conta do lado
de fora, assinando a saída do detector, que é o que prova o cano inteiro.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import zmq

logger = logging.getLogger(__name__)

RAW_PACKET_TOPIC = b"raw_packet"

# O último pacote de um replay sai do detector depois do último lote de IQ:
# a rajada inteira precisa passar por uma janela do demodulador (0,5 s).
DEFAULT_GRACE_S = 2.0


class PacketCounter:
    def __init__(self, address: str, grace_s: float = DEFAULT_GRACE_S) -> None:
        self.address = address
        self._grace_s = grace_s
        self._count = 0
        self._bit_offsets: list[int] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="packet-counter")

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(2.0)
        # PUB descarta o que publica antes de o SUB terminar de conectar.
        time.sleep(0.3)

    def finish(self) -> int:
        """Espera os pacotes em trânsito, para, e devolve a contagem."""
        time.sleep(self._grace_s)
        self._stop.set()
        self._thread.join(timeout=2.0)
        return self.count

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def bit_offsets(self) -> list[int]:
        with self._lock:
            return list(self._bit_offsets)

    def _run(self) -> None:
        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, RAW_PACKET_TOPIC)
        socket.setsockopt(zmq.RCVTIMEO, 200)
        socket.connect(self.address)
        self._ready.set()

        while not self._stop.is_set():
            try:
                frames = socket.recv_multipart()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                break
            if len(frames) != 3 or frames[0] != RAW_PACKET_TOPIC:
                continue
            try:
                offset = int(json.loads(frames[1])["bit_offset"])
            except (ValueError, KeyError, TypeError):
                offset = None
            with self._lock:
                self._count += 1
                if offset is not None:
                    self._bit_offsets.append(offset)

        socket.close(linger=0)
        context.term()
