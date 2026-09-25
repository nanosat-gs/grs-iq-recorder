"""Testes do RtlTcpIqSource — SDR via rede, sem gqrx, comandável por código.

O duplo aqui não é um mock em memória: é um socket TCP de verdade, escrito do
zero a partir do protocolo `librtlsdr/rtl_tcp.c` (não da implementação do
adapter). Se os dois lados fossem escritos a partir da mesma suposição errada
sobre o protocolo, um mock em memória concordaria com o adapter e nenhum teste
pegaria a divergência — o mesmo argumento que já apareceu com o simulador de
2GFSK e o syncword do NGHam.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import numpy as np
import pytest
import zmq

from iq_recorder.adapters.rtltcp_iq_source import (
    CMD_SET_FREQUENCY,
    CMD_SET_GAIN,
    CMD_SET_GAIN_MODE,
    CMD_SET_SAMPLE_RATE,
    RtlTcpIqSource,
    pack_command,
)

DONGLE_INFO = b"RTL0" + struct.pack(">II", 1, 29)  # tuner_type=1 (E4000), 29 ganhos


class FakeRtlTcp:
    """Um servidor rtl_tcp mínimo, em TCP de verdade.

    Manda o cabeçalho no accept(), grava todo comando de 5 bytes recebido (sem
    nunca responder — o protocolo real também não responde), e despeja os
    bytes de IQ que o teste mandar escrever.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]

        self.commands: list[bytes] = []
        self._conn: socket.socket | None = None
        self._to_send: list[bytes] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        conn.sendall(DONGLE_INFO)
        self._conn = conn
        self._ready.set()

        conn.settimeout(0.05)
        while True:
            # Lado 1: grava comandos que o cliente manda.
            try:
                chunk = conn.recv(5)
                if chunk:
                    self.commands.append(chunk)
                elif chunk == b"":
                    break
            except (socket.timeout, OSError):
                pass

            # Lado 2: despeja IQ que o teste enfileirou.
            with self._lock:
                pending = self._to_send
                self._to_send = []
            for payload in pending:
                try:
                    conn.sendall(payload)
                except OSError:
                    return

    def wait_ready(self, timeout: float = 2.0) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("servidor falso não aceitou conexão")

    def push_iq(self, payload: bytes) -> None:
        with self._lock:
            self._to_send.append(payload)

    def close(self) -> None:
        self._listener.close()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass


@pytest.fixture
def server():
    srv = FakeRtlTcp()
    yield srv
    srv.close()


def iq_bytes(pairs: list[tuple[int, int]]) -> bytes:
    """[(i_byte, q_byte), ...] -> bytes intercalados, como o rtl_tcp manda."""
    return bytes(byte for pair in pairs for byte in pair)


# --- o protocolo, byte a byte -------------------------------------------------


def test_le_o_cabecalho_e_extrai_tipo_e_contagem_de_ganho(server):
    source = RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                             frequency_hz=145_900_000.0)
    server.wait_ready()

    assert source.tuner_type == 1
    assert source.tuner_gain_count == 29
    source.close()


def test_cabecalho_sem_magic_rtl0_e_recusado():
    """Conectar em qualquer outra coisa (a porta errada, um serviço que não é
    rtl_tcp) tem de falhar alto, não produzir IQ de um cabeçalho lido errado.

    Listener PRÓPRIO, e não o da fixture `server`: reaproveitar aquele
    listener disputaria o accept() com a thread que a fixture já deixou
    esperando desde o __init__, e a thread antiga — que já estava bloqueada
    em accept() — ganharia a corrida quase sempre, mandando o cabeçalho
    CORRETO em vez do falso. O bug ficava escondido atrás de uma corrida.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def fake_other_protocol():
        conn, _ = listener.accept()
        conn.sendall(b"HTTP/1.1 400")  # 12 bytes de outra coisa qualquer
        conn.close()

    thread = threading.Thread(target=fake_other_protocol, daemon=True)
    thread.start()

    try:
        with pytest.raises(ConnectionError, match="RTL0"):
            RtlTcpIqSource("127.0.0.1", port, sample_rate_hz=240_000,
                           frequency_hz=145_900_000.0)
    finally:
        listener.close()


def test_configura_taxa_frequencia_e_ganho_automatico_no_boot(server):
    source = RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                             frequency_hz=145_900_000.0)
    server.wait_ready()
    time.sleep(0.15)  # dá tempo do laço de leitura do fake gravar os comandos

    assert pack_command(CMD_SET_SAMPLE_RATE, 240_000) in server.commands
    assert pack_command(CMD_SET_FREQUENCY, 145_900_000) in server.commands
    assert pack_command(CMD_SET_GAIN_MODE, 0) in server.commands  # automático
    source.close()


def test_ganho_manual_manda_gain_mode_e_gain_em_decimos_de_db(server):
    source = RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                             frequency_hz=145_900_000.0, gain_db=28.5)
    server.wait_ready()
    time.sleep(0.15)

    assert pack_command(CMD_SET_GAIN_MODE, 1) in server.commands
    assert pack_command(CMD_SET_GAIN, 285) in server.commands  # 28.5 dB -> 285
    source.close()


def test_pack_command_confere_com_o_protocolo_upstream():
    """1 byte de id + 4 bytes de parâmetro, big-endian — direto de
    librtlsdr/src/rtl_tcp.c, não deduzido."""
    assert pack_command(0x01, 145_900_000) == b"\x01" + (145_900_000).to_bytes(4, "big")
    assert pack_command(0x04, 285) == bytes([0x04, 0x00, 0x00, 0x01, 0x1D])


def test_pack_command_recusa_parametro_fora_de_uint32():
    with pytest.raises(ValueError, match="uint32"):
        pack_command(CMD_SET_FREQUENCY, 2**32)


def test_ganho_negativo_e_recusado(server):
    with pytest.raises(ValueError, match="gain_db"):
        RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                       frequency_hz=145_900_000.0, gain_db=-3.0)


# --- normalização do IQ -------------------------------------------------------


def test_a_normalizacao_e_identica_ao_grs_iq_rx_em_c(server):
    """(byte - 127.5) / 127.5 — a mesma conta de main.c. Divergir aqui
    produziria um IQ que passa em todo teste de forma mas calibra diferente
    do receptor real."""
    source = RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                             frequency_hz=145_900_000.0, read_chunk_bytes=4)
    server.wait_ready()

    # I=0 (mínimo), Q=255 (máximo), I=128 (~zero), Q=127 (~zero, do outro lado)
    server.push_iq(iq_bytes([(0, 255), (128, 127)]))

    block = next(source.blocks())
    samples = np.frombuffer(block.data, dtype=np.complex64)

    esperado_i0 = (0 - 127.5) / 127.5
    esperado_q0 = (255 - 127.5) / 127.5
    assert samples[0].real == pytest.approx(esperado_i0, abs=1e-5)
    assert samples[0].imag == pytest.approx(esperado_q0, abs=1e-5)
    source.close()


def test_lote_grande_preserva_todas_as_amostras(server):
    n_amostras = 500
    source = RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                             frequency_hz=145_900_000.0,
                             read_chunk_bytes=n_amostras * 2)
    server.wait_ready()

    pares = [(i % 256, (i * 7) % 256) for i in range(n_amostras)]
    server.push_iq(iq_bytes(pares))

    block = next(source.blocks())
    samples = np.frombuffer(block.data, dtype=np.complex64)

    assert len(samples) == n_amostras
    source.close()


def test_read_chunk_impar_e_recusado():
    """Um lote ímpar cortaria um par I/Q ao meio na fronteira — a mesma
    armadilha que o loteamento do grs-iq-rx em C já documenta."""
    with pytest.raises(ValueError, match="par"):
        RtlTcpIqSource("127.0.0.1", 1, sample_rate_hz=240_000,
                       frequency_hz=145_900_000.0, read_chunk_bytes=5)


def test_taxa_zero_e_recusada():
    with pytest.raises(ValueError, match="sample_rate_hz"):
        RtlTcpIqSource("127.0.0.1", 1, sample_rate_hz=0, frequency_hz=1.0)


# --- sintonia via :5557, o mesmo contrato do grs-sdr-sim ----------------------


def test_tune_manda_set_frequency_pela_mesma_conexao(server):
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    tune_port = publisher.bind_to_random_port("tcp://127.0.0.1")

    source = RtlTcpIqSource(
        "127.0.0.1", server.port, sample_rate_hz=240_000, frequency_hz=145_900_000.0,
        tune_address=f"tcp://127.0.0.1:{tune_port}",
    )
    server.wait_ready()
    time.sleep(0.3)  # slow joiner: o SUB precisa terminar de conectar

    publisher.send_multipart([b"tune", b"146400000"])
    time.sleep(0.3)

    assert pack_command(CMD_SET_FREQUENCY, 146_400_000) in server.commands

    source.close()
    publisher.close()
    context.term()


def test_tune_ilegivel_e_ignorado_sem_derrubar_o_adapter(server):
    """Uma mensagem tune corrompida não pode travar a leitura de IQ — o
    caminho de dados é mais importante do que uma sintonia perdida."""
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    tune_port = publisher.bind_to_random_port("tcp://127.0.0.1")

    source = RtlTcpIqSource(
        "127.0.0.1", server.port, sample_rate_hz=240_000, frequency_hz=145_900_000.0,
        tune_address=f"tcp://127.0.0.1:{tune_port}", read_chunk_bytes=2,
    )
    server.wait_ready()
    time.sleep(0.3)

    publisher.send_multipart([b"tune", b"nao-e-um-numero"])
    time.sleep(0.2)
    server.push_iq(iq_bytes([(100, 150)]))  # 2 bytes = 1 amostra = read_chunk_bytes

    block = next(source.blocks())
    assert len(block.data) == 8  # uma amostra cf32_le sobreviveu

    source.close()
    publisher.close()
    context.term()


# --- fechamento -----------------------------------------------------------


def test_close_interrompe_o_laco_de_leitura(server):
    source = RtlTcpIqSource("127.0.0.1", server.port, sample_rate_hz=240_000,
                             frequency_hz=145_900_000.0, read_chunk_bytes=4)
    server.wait_ready()

    iterator = source.blocks()
    source.close()

    with pytest.raises(StopIteration):
        next(iterator)
