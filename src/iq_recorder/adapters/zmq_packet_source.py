"""Adapter de entrada: raw packets do detector de syncword (:5558).

Implementa `PacketSource`. O envelope, definido no `service.c` do detector:

    [0] tópico    "raw_packet"
    [1] cabeçalho JSON de uma linha
    [2] payload   bytes empacotados MSB-first, os bits depois do syncword
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterator

import zmq

from iq_recorder.domain.models import RawPacket

logger = logging.getLogger(__name__)

RAW_PACKET_TOPIC = b"raw_packet"

# Quanto esperar por um pacote antes de devolver None ao laço. Curto o
# bastante para o buffer ser gravado em ~1 s e um SIGTERM ser atendido logo.
DEFAULT_IDLE_TIMEOUT_MS = 500

# Sem limite, o ZMQ enfileira sem teto enquanto o banco está fora. O limite
# fica no buffer da aplicação, que conta o que descarta; aqui só se evita que
# a fila do socket cresça sem ninguém ver.
DEFAULT_RCVHWM = 100_000


def parse_raw_packet(frames: list[bytes], received_at: datetime) -> RawPacket | None:
    """Um envelope do detector -> RawPacket, ou None se não for um."""
    if len(frames) != 3 or frames[0] != RAW_PACKET_TOPIC:
        return None

    try:
        header = json.loads(frames[1])
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(header, dict):
        return None

    return RawPacket(
        received_at=received_at,
        payload=bytes(frames[2]),
        header=header,
        detector_seq=_int(header.get("seq")),
        bit_offset=_int(header.get("bit_offset")),
        detected_at=_timestamp(header.get("detected_at")),
        syncword=header.get("syncword") if isinstance(header.get("syncword"), str) else None,
        max_sync_errors=_int(header.get("max_sync_errors")),
    )


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ZmqPacketSource:
    """Implementa PacketSource assinando o PUB do detector."""

    def __init__(self, address: str, idle_timeout_ms: int = DEFAULT_IDLE_TIMEOUT_MS,
                 context: zmq.Context | None = None) -> None:
        self.address = address
        self.malformed = 0

        self._owns_context = context is None
        self._context = context if context is not None else zmq.Context()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, RAW_PACKET_TOPIC)
        self._socket.setsockopt(zmq.RCVTIMEO, idle_timeout_ms)
        self._socket.setsockopt(zmq.RCVHWM, DEFAULT_RCVHWM)
        self._socket.connect(address)
        self._closed = False

    def packets(self) -> Iterator[RawPacket | None]:
        while not self._closed:
            try:
                frames = self._socket.recv_multipart()
            except zmq.Again:
                yield None
                continue
            except zmq.ZMQError:
                if self._closed:
                    return
                raise

            packet = parse_raw_packet(frames, datetime.now(timezone.utc))
            if packet is None:
                self.malformed += 1
                logger.warning("Envelope de raw packet malformado (%d até agora), ignorado.",
                               self.malformed)
                continue
            yield packet

    def close(self) -> None:
        self._closed = True
        self._socket.close(linger=0)
        if self._owns_context:
            self._context.term()
