"""Adapter de entrada: SDR via rede, sem GUI, sintonizável por código.

Implementa `IqStreamSource`. Conecta num `rtl_tcp` — o servidor de linha de
comando do mesmo projeto `librtlsdr` que o `grs-iq-rx` já usa — e devolve IQ
real capturado, não reconstruído.

Por que `rtl_tcp` e não gqrx: `rtl_tcp` é puro protocolo, sem janela, sem
passo manual. Só ele precisa de acesso USB ao dongle; a partir daí, sintonizar
é um comando de 5 bytes na mesma conexão TCP que já está entregando amostras.
`gqrx` resolveria o mesmo problema de rede, mas exigiria alguém abrindo a
janela, plugando o SDR nela e marcando caixinhas antes de qualquer API
funcionar — o oposto de "comandado por código".

## O protocolo, direto da fonte

Confirmado em `librtlsdr/src/rtl_tcp.c` (upstream, não vendorizado aqui):

    conexão TCP -> 12 bytes: b"RTL0" + tuner_type (u32 BE) + n_gains (u32 BE)
    depois disso: fluxo contínuo de IQ cru, uint8 intercalado (I, Q, I, Q, ...)

    a qualquer momento, o CLIENTE pode mandar um comando de 5 bytes:
        1 byte  id do comando
        4 bytes parâmetro, big-endian

    0x01 SET_FREQUENCY     Hz (u32)
    0x02 SET_SAMPLE_RATE   Hz (u32)
    0x03 SET_GAIN_MODE     0=automático, 1=manual (u32)
    0x04 SET_GAIN          décimos de dB (u32)

O servidor nunca responde aos comandos — não há RPRT, não há eco. O único
jeito de saber se um SET_FREQUENCY "pegou" é observar o sinal mudar de lugar
no espectro (é para isso que existe `NumpyPsdView` e `inspect`).

## Normalização, igual ao grs-iq-rx

    iq.i = (byte[0] - 127.5) / 127.5
    iq.q = (byte[1] - 127.5) / 127.5

A mesma conta de `main.c`. Usar outra normalização aqui produziria um IQ
tecnicamente válido mas numericamente diferente do que o receptor C entrega
— e duas fontes "equivalentes" que na prática calibram o sinal diferente são
piores que uma só, porque o defeito só aparece na comparação.

## Sintonia: o mesmo contrato do grs-sdr-sim

Assina `tune` em `:5557` — a mesma mensagem `[b"tune", b"<Hz>"]` que o
`frequency-synthesizer` publica e que o `grs-sdr-sim` já consome. Um
`RtlTcpIqSource` configurado apontando para essa porta é, do ponto de vista do
Station Manager, indistinguível do simulador: os dois obedecem ao mesmo
comando, um sintonizando de verdade, o outro sintonizando um espectro
imaginado.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from datetime import datetime, timezone
from typing import Iterator

import numpy as np
import zmq

from iq_recorder.domain.models import IqBlock

logger = logging.getLogger(__name__)

# Cabeçalho fixo que o rtl_tcp manda ao aceitar a conexão.
DONGLE_INFO_MAGIC = b"RTL0"
DONGLE_INFO_SIZE = 12  # 4 (magic) + 4 (tuner_type) + 4 (n_gains), tudo big-endian

# IDs de comando, só os quatro que este adapter usa. A lista completa (GPIO,
# I2C, bias-tee, offset tuning...) não tem lugar aqui: o resto é controle de
# hardware que esta estação não decide por enquanto.
CMD_SET_FREQUENCY = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03
CMD_SET_GAIN = 0x04

# Cada amostra são 2 bytes crus (I, Q); um lote grande evita send() demais no
# laço de leitura, sem seguar tanto tempo que o `tune` demore a ser aplicado.
DEFAULT_READ_CHUNK_BYTES = 32 * 1024

DEFAULT_CONNECT_TIMEOUT_S = 5.0

# Sem tópico, subscrição vazia: mesma convenção do resto do cano. O
# `frequency-synthesizer` prefixa cada mensagem com o tópico "tune" OU
# "freq"/"doppler" dependendo de quem fala — aqui filtramos só "tune", porque
# é o único que este adapter sabe obedecer.
TUNE_TOPIC = b"tune"
DEFAULT_TUNE_RECEIVE_TIMEOUT_MS = 500


def pack_command(command_id: int, parameter: int) -> bytes:
    """Monta os 5 bytes de um comando rtl_tcp: 1 byte id + 4 bytes BE."""
    if not 0 <= parameter <= 0xFFFFFFFF:
        raise ValueError(f"parâmetro fora de uint32: {parameter}")

    return struct.pack(">BI", command_id, parameter)


class _TuneListener(threading.Thread):
    """Assina `:5557` e traduz cada `tune` num SET_FREQUENCY, pela mesma
    conexão TCP que está recebendo IQ.

    Em thread própria pelo mesmo motivo do `grs-sdr-sim`: o laço de leitura de
    IQ é o caminho de tempo real, e um `recv()` bloqueante de ZMQ no meio dele
    atrasaria amostras — o que apareceria como falha de demodulação, o
    sintoma mais caro de diagnosticar.
    """

    def __init__(self, address: str, sock: socket.socket, lock: threading.Lock) -> None:
        super().__init__(daemon=True, name="rtltcp-tune-listener")
        self._address = address
        self._sock = sock
        self._lock = lock
        self._context = zmq.Context()
        self._zmq_socket = self._context.socket(zmq.SUB)
        self._zmq_socket.setsockopt(zmq.SUBSCRIBE, TUNE_TOPIC)
        self._zmq_socket.setsockopt(zmq.RCVTIMEO, DEFAULT_TUNE_RECEIVE_TIMEOUT_MS)
        self._zmq_socket.connect(address)
        self._stopping = threading.Event()

    def run(self) -> None:
        logger.info("Ouvindo tune em %s", self._address)

        while not self._stopping.is_set():
            try:
                frames = self._zmq_socket.recv_multipart()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                break

            if len(frames) != 2:
                continue

            try:
                frequency_hz = int(float(frames[1].decode()))
            except (ValueError, UnicodeDecodeError):
                logger.warning("tune ilegível: %r", frames[1])
                continue

            command = pack_command(CMD_SET_FREQUENCY, frequency_hz)

            # Trava o socket: o laço de leitura de IQ também usa a mesma
            # conexão, e enviar um comando de 5 bytes no meio de um recv()
            # alheio corromperia o fluxo se as duas threads escrevessem ao
            # mesmo tempo. sendall() é curto — não há risco real de prender a
            # leitura por muito tempo.
            with self._lock:
                try:
                    self._sock.sendall(command)
                except OSError:
                    logger.exception("Falha ao enviar SET_FREQUENCY")
                    continue

            logger.info("tune -> %.4f MHz", frequency_hz / 1e6)

        self._zmq_socket.close()
        self._context.term()

    def stop(self) -> None:
        self._stopping.set()


class RtlTcpIqSource:
    """Implementa IqStreamSource conectando num servidor rtl_tcp.

    IQ real, capturado — não reconstruído a partir de áudio. A amplitude é a
    do sinal de verdade, e nada além do próprio AGC do RTL-SDR (se ligado) foi
    aplicado a ela.
    """

    def __init__(
        self,
        host: str,
        port: int,
        sample_rate_hz: int,
        frequency_hz: float,
        gain_db: float | None = None,
        tune_address: str | None = None,
        read_chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz precisa ser positivo, veio {sample_rate_hz}")
        if frequency_hz <= 0:
            raise ValueError(f"frequency_hz precisa ser positivo, veio {frequency_hz}")
        if read_chunk_bytes < 2 or read_chunk_bytes % 2 != 0:
            # Ímpar cortaria um par I/Q ao meio na fronteira do lote — a
            # mesma armadilha que o loteamento do grs-iq-rx (C) já documenta.
            raise ValueError("read_chunk_bytes precisa ser par e >= 2")

        self.host = host
        self.port = port
        self.sample_rate_hz = sample_rate_hz
        self.frequency_hz = frequency_hz
        self.gain_db = gain_db
        self._read_chunk_bytes = read_chunk_bytes

        self._sock = socket.create_connection((host, port), timeout=connect_timeout_s)
        self._sock.settimeout(None)  # volta a bloqueante para o laço de leitura
        self._send_lock = threading.Lock()

        self.tuner_type, self.tuner_gain_count = self._read_dongle_info()

        self._configure()

        self._tuner: _TuneListener | None = None
        if tune_address:
            self._tuner = _TuneListener(tune_address, self._sock, self._send_lock)
            self._tuner.start()

        self._sequence = 0
        self._closed = False

    # --- handshake e configuração --------------------------------------------

    def _read_dongle_info(self) -> tuple[int, int]:
        header = self._recv_exact(DONGLE_INFO_SIZE)

        if header[:4] != DONGLE_INFO_MAGIC:
            raise ConnectionError(
                f"cabeçalho inesperado de {self.host}:{self.port}: {header[:4]!r} "
                f'(esperava {DONGLE_INFO_MAGIC!r} — isto é mesmo um rtl_tcp?)'
            )

        tuner_type, gain_count = struct.unpack(">II", header[4:])

        return tuner_type, gain_count

    def _configure(self) -> None:
        with self._send_lock:
            self._sock.sendall(pack_command(CMD_SET_SAMPLE_RATE, self.sample_rate_hz))
            self._sock.sendall(pack_command(CMD_SET_FREQUENCY, int(self.frequency_hz)))

            if self.gain_db is None:
                self._sock.sendall(pack_command(CMD_SET_GAIN_MODE, 0))
            else:
                self._sock.sendall(pack_command(CMD_SET_GAIN_MODE, 1))
                # rtl_tcp quer décimos de dB, e ganho negativo não existe no
                # RTL-SDR — um valor negativo aqui é erro de configuração, não
                # um ganho baixo válido.
                if self.gain_db < 0:
                    raise ValueError(f"gain_db não pode ser negativo, veio {self.gain_db}")
                self._sock.sendall(pack_command(CMD_SET_GAIN, int(round(self.gain_db * 10))))

    def _recv_exact(self, n: int) -> bytes:
        """Lê exatamente `n` bytes, ou explode — TCP não garante um recv() por
        mensagem, e tratar uma leitura parcial como completa corromperia o
        cabeçalho ou desalinharia o fluxo de amostras."""
        chunks = []
        remaining = n

        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError(
                    f"conexão com {self.host}:{self.port} fechou no meio de uma leitura "
                    f"({n - remaining}/{n} bytes)"
                )
            chunks.append(chunk)
            remaining -= len(chunk)

        return b"".join(chunks)

    # --- IqStreamSource -------------------------------------------------------

    def blocks(self) -> Iterator[IqBlock]:
        """Lotes de IQ, indefinidamente, convertidos para cf32_le.

        A normalização é a MESMA do `grs-iq-rx` em C: `(byte - 127.5) / 127.5`.
        Divergir aqui produziria um IQ que passa em todo teste de forma mas
        calibra diferente do receptor de verdade — o pior tipo de
        inconsistência, porque só aparece quando alguém compara os dois lado
        a lado.
        """
        while not self._closed:
            try:
                raw = self._recv_exact(self._read_chunk_bytes)
            except (ConnectionError, OSError):
                if self._closed:
                    break
                raise

            samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            iq = ((samples - 127.5) / 127.5).astype(np.float32)
            complex_samples = iq[0::2] + 1j * iq[1::2]

            yield IqBlock(
                data=complex_samples.astype(np.complex64).tobytes(),
                sequence=self._sequence,
                received_at=datetime.now(timezone.utc),
            )
            self._sequence += 1

    def close(self) -> None:
        self._closed = True

        if self._tuner is not None:
            self._tuner.stop()

        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # já pode estar fechado do outro lado
        self._sock.close()
