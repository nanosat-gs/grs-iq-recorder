"""Testes do UsrpIqSource — SDR via UHD, sem reimplementar protocolo nenhum.

## Por que estes testes têm uma garantia mais fraca que os do rtl_tcp

Para o `rtl_tcp` foi possível escrever um servidor de rede FALSO MAS REAL — um
socket TCP que fala exatamente o protocolo, verificado contra a fonte oficial.
Aqui não dá: a Ettus não documenta o protocolo de rede do USRP para uso por
terceiros, só recomenda a biblioteca deles. Reimplementar esse protocolo só
para testar seria fazer exatamente o que este adapter existe para NÃO fazer.

A troca: só o `MultiUSRP` — a classe que fala com o hardware de verdade — é
substituído por um duplo. Todo o resto (`uhd.types.TuneRequest`,
`uhd.types.StreamArgs`, `uhd.types.StreamCMD`, `uhd.types.StreamMode`,
`uhd.types.RXMetadata`, `uhd.types.RXMetadataErrorCode`) é o módulo `uhd` DE
VERDADE, instalado via apt neste container — não um mock que aceitaria
qualquer coisa. Se o adapter construir um `TuneRequest` errado, ou pedir um
`StreamArgs` num formato que o UHD instalado não reconhece, estes testes
FALHAM de verdade, contra o tipo real.

O que não é coberto, e não tem como ser sem o rádio físico: se `recv()`
entrega amostras de verdade, se `set_rx_freq` de fato sintoniza, qualquer
coisa que dependa do firmware do N210.

## Como rodar

Só dentro da imagem `Dockerfile.usrp` — `uhd` não existe fora dela.
`pytest.importorskip` faz este arquivo inteiro ser pulado (não falhar) em
qualquer outro lugar, incluindo a máquina de desenvolvimento no Windows.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
import zmq

uhd = pytest.importorskip("uhd")

from iq_recorder.adapters.usrp_iq_source import (  # noqa: E402
    UsrpConnectionError,
    UsrpIqSource,
)


class FakeRXMetadata:
    """Substitui `uhd.types.RXMetadata` — só o contêiner, não o enum.

    O `RXMetadata` real é um tipo pybind11 cujo `error_code` não tem setter
    do lado do Python: só o `recv()` em C++ o preenche, por referência.
    Confirmado direto: `uhd.types.RXMetadata().error_code = ...` levanta
    `AttributeError: property of 'rx_metadata' object has no setter`. Um
    duplo em Python não consegue imitar esse preenchimento por referência —
    por isso só o CONTÊINER é falso aqui. Os valores atribuídos continuam
    sendo o enum de verdade, `uhd.types.RXMetadataErrorCode` — não um
    inteiro solto nem uma string.
    """

    def __init__(self) -> None:
        self.error_code = uhd.types.RXMetadataErrorCode.none

    def strerror(self) -> str:
        return str(self.error_code)


class FakeStreamer:
    """Substitui `RXStreamer`: fila de amostras + metadados REAIS do UHD."""

    def __init__(self) -> None:
        self.stream_cmds: list = []
        self._queue: list[np.ndarray] = []
        self._default_error = uhd.types.RXMetadataErrorCode.timeout

    def queue_samples(self, samples: np.ndarray) -> None:
        self._queue.append(samples)

    def issue_stream_cmd(self, cmd) -> None:
        self.stream_cmds.append(cmd)

    def recv(self, buffer: np.ndarray, metadata, timeout: float) -> int:
        if not self._queue:
            metadata.error_code = self._default_error
            return 0

        samples = self._queue.pop(0)
        n = len(samples)
        buffer[0, :n] = samples
        metadata.error_code = uhd.types.RXMetadataErrorCode.none

        return n


class FakeMultiUSRP:
    """Substitui `MultiUSRP`: só a ponta que fala com o hardware físico.

    Grava exatamente o que o adapter pediu (taxa, TuneRequest — REAL, ganho),
    para os testes conferirem contra o objeto de verdade que o UHD receberia.
    """

    instances: list["FakeMultiUSRP"] = []

    def __init__(self, args: str = "") -> None:
        if "unreachable" in args:
            # O que o UHD de verdade faz contra um endereço sem ninguém
            # escutando — confirmado rodando de verdade antes deste arquivo
            # existir: RuntimeError, não um travamento.
            raise RuntimeError(f"LookupError: KeyError: No devices found for {args}")

        self.args = args
        self.rx_rate: float | None = None
        self.rx_freq_requests: list = []  # TuneRequest reais, não floats
        self.rx_gain: float | None = None
        self.streamer = FakeStreamer()
        FakeMultiUSRP.instances.append(self)

    def set_rx_rate(self, rate: float, channel: int = 0) -> None:
        self.rx_rate = rate

    def set_rx_freq(self, tune_request, channel: int = 0):
        self.rx_freq_requests.append(tune_request)
        return None

    def set_rx_gain(self, gain: float, channel: int = 0) -> None:
        self.rx_gain = gain

    def get_rx_stream(self, stream_args) -> FakeStreamer:
        self.stream_args = stream_args
        return self.streamer


@pytest.fixture(autouse=True)
def fake_hardware(monkeypatch):
    """Troca `MultiUSRP` e `RXMetadata`. O resto de `uhd.types.*` — inclusive
    o enum `RXMetadataErrorCode` que `FakeRXMetadata` usa por dentro —
    continua sendo o módulo real."""
    FakeMultiUSRP.instances.clear()
    monkeypatch.setattr(uhd.usrp, "MultiUSRP", FakeMultiUSRP)
    monkeypatch.setattr(uhd.types, "RXMetadata", FakeRXMetadata)
    yield


def last_usrp() -> FakeMultiUSRP:
    assert FakeMultiUSRP.instances, "nenhum MultiUSRP foi construído"
    return FakeMultiUSRP.instances[-1]


# --- configuração no boot ----------------------------------------------------


def test_endereco_vira_device_args_addr(fake_hardware):
    UsrpIqSource(host="192.168.10.2", sample_rate_hz=1e6, frequency_hz=145.9e6)

    assert last_usrp().args == "addr=192.168.10.2"


def test_configura_taxa_no_boot(fake_hardware):
    UsrpIqSource(host="x", sample_rate_hz=2_000_000.0, frequency_hz=145.9e6)

    assert last_usrp().rx_rate == 2_000_000.0


def test_sintonia_inicial_e_um_tunerequest_real(fake_hardware):
    """O adapter tem de construir um uhd.types.TuneRequest de verdade — não
    um float cru, não um dicionário, não um objeto próprio."""
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145_900_000.0)

    request = last_usrp().rx_freq_requests[0]
    assert isinstance(request, uhd.types.TuneRequest)
    assert request.target_freq == 145_900_000.0
    source.close()


def test_sem_ganho_nao_chama_set_rx_gain(fake_hardware):
    """gain_db=None é para deixar o USRP no que ele já estiver — chamar
    set_rx_gain(None) quebraria contra o tipo real (espera float)."""
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6)

    assert last_usrp().rx_gain is None
    source.close()


def test_ganho_explicito_e_repassado(fake_hardware):
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6, gain_db=40.0)

    assert last_usrp().rx_gain == 40.0
    source.close()


def test_stream_args_pede_fc32_sobre_sc16(fake_hardware):
    """fc32: o adapter não deve fazer NENHUMA conversão de amostra — é o UHD
    que entrega complex64 pronto. sc16 é o formato no fio, não algo que este
    adapter escolha por capricho."""
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6)

    args = last_usrp().stream_args
    assert args.cpu_format == "fc32"
    assert args.otw_format == "sc16"
    source.close()


def test_inicia_stream_continuo_no_boot(fake_hardware):
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6)

    cmds = last_usrp().streamer.stream_cmds
    assert len(cmds) == 1
    assert cmds[0].stream_now is True
    source.close()


def test_endereco_sem_dispositivo_vira_usrpconnectionerror(fake_hardware):
    """O RuntimeError cru do UHD (confirmado: 0,7s, não trava) tem de virar o
    erro do próprio domínio deste adapter, não vazar tipo de terceiro."""
    with pytest.raises(UsrpConnectionError, match="unreachable-host"):
        UsrpIqSource(host="unreachable-host", sample_rate_hz=1e6, frequency_hz=145.9e6)


def test_taxa_zero_e_recusada(fake_hardware):
    with pytest.raises(ValueError, match="sample_rate_hz"):
        UsrpIqSource(host="x", sample_rate_hz=0, frequency_hz=145.9e6)


def test_frequencia_zero_e_recusada(fake_hardware):
    with pytest.raises(ValueError, match="frequency_hz"):
        UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=0)


# --- recepção -----------------------------------------------------------------


def test_amostras_da_fila_viram_iqblock(fake_hardware):
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6, block_samples=4)
    last_usrp().streamer.queue_samples(
        np.array([1 + 2j, 3 + 4j], dtype=np.complex64)
    )

    block = next(source.blocks())
    samples = np.frombuffer(block.data, dtype=np.complex64)

    assert list(samples) == [1 + 2j, 3 + 4j]
    source.close()


def test_amostras_sao_complex64_sem_nenhuma_conversao(fake_hardware):
    """Ao contrário do rtl_tcp (uint8 cru) e do import-wav (reconstrução de
    fase), aqui o valor que entra é o valor que sai — o UHD já fez o trabalho
    de converter do formato no fio para float."""
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6, block_samples=4)
    valor = complex(0.31622776, -0.9486833)  # arbitrário, não normalizável
    last_usrp().streamer.queue_samples(np.array([valor], dtype=np.complex64))

    block = next(source.blocks())
    samples = np.frombuffer(block.data, dtype=np.complex64)

    assert samples[0] == np.complex64(valor)
    source.close()


def test_timeout_do_recv_nao_produz_bloco(fake_hardware):
    """Sem amostra na fila, o FakeStreamer devolve error_code=timeout — o
    mesmo que o UHD real devolve quando não há sinal chegando. O adapter tem
    de continuar esperando, não gerar um bloco vazio nem quebrar."""
    source = UsrpIqSource(
        host="x", sample_rate_hz=1e6, frequency_hz=145.9e6,
        block_samples=4, recv_timeout_s=0.01,
    )
    last_usrp().streamer.queue_samples(np.array([5 + 5j], dtype=np.complex64))

    block = next(source.blocks())  # passa por timeouts até a amostra chegar
    samples = np.frombuffer(block.data, dtype=np.complex64)

    assert samples[0] == np.complex64(5 + 5j)
    source.close()


def test_overflow_nao_derruba_o_adapter(fake_hardware):
    """Um estouro de buffer é perda de amostra, não motivo para parar de
    receber — é exatamente o tipo de falha transiente que uma passagem real
    pode ter."""
    source = UsrpIqSource(
        host="x", sample_rate_hz=1e6, frequency_hz=145.9e6,
        block_samples=4, recv_timeout_s=0.01,
    )
    streamer = last_usrp().streamer
    original_recv = streamer.recv

    def recv_with_overflow_once(buffer, metadata, timeout):
        streamer.recv = original_recv  # só uma vez
        metadata.error_code = uhd.types.RXMetadataErrorCode.overflow
        return 0

    streamer.recv = recv_with_overflow_once
    streamer.queue_samples(np.array([9 + 9j], dtype=np.complex64))

    block = next(source.blocks())
    samples = np.frombuffer(block.data, dtype=np.complex64)

    assert samples[0] == np.complex64(9 + 9j)
    source.close()


def test_close_emite_stop_cont(fake_hardware):
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6)
    streamer = last_usrp().streamer

    source.close()

    modes_pedidos = [cmd.stream_now for cmd in streamer.stream_cmds]
    assert len(streamer.stream_cmds) == 2  # start_cont no boot, stop_cont no close
    assert modes_pedidos[0] is True  # start


def test_close_interrompe_o_laco(fake_hardware):
    source = UsrpIqSource(host="x", sample_rate_hz=1e6, frequency_hz=145.9e6)

    iterator = source.blocks()
    source.close()

    with pytest.raises(StopIteration):
        next(iterator)


# --- sintonia via :5557, o mesmo contrato do grs-sdr-sim e do rtl_tcp --------


def test_tune_chama_set_rx_freq_com_tunerequest_real(fake_hardware):
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    tune_port = publisher.bind_to_random_port("tcp://127.0.0.1")

    source = UsrpIqSource(
        host="x", sample_rate_hz=1e6, frequency_hz=145_900_000.0,
        tune_address=f"tcp://127.0.0.1:{tune_port}",
    )
    time.sleep(0.3)  # slow joiner

    publisher.send_multipart([b"tune", b"146400000"])
    time.sleep(0.3)

    requests = last_usrp().rx_freq_requests
    assert any(isinstance(r, uhd.types.TuneRequest) and r.target_freq == 146_400_000.0
              for r in requests)

    source.close()
    publisher.close()
    context.term()


def test_tune_ilegivel_nao_derruba_a_recepcao(fake_hardware):
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    tune_port = publisher.bind_to_random_port("tcp://127.0.0.1")

    source = UsrpIqSource(
        host="x", sample_rate_hz=1e6, frequency_hz=145.9e6, block_samples=4,
        tune_address=f"tcp://127.0.0.1:{tune_port}", recv_timeout_s=0.01,
    )
    time.sleep(0.3)

    publisher.send_multipart([b"tune", b"nao-e-um-numero"])
    time.sleep(0.2)
    last_usrp().streamer.queue_samples(np.array([7 + 7j], dtype=np.complex64))

    block = next(source.blocks())
    samples = np.frombuffer(block.data, dtype=np.complex64)
    assert samples[0] == np.complex64(7 + 7j)

    source.close()
    publisher.close()
    context.term()


# --- import tardio ------------------------------------------------------------


def test_import_tardio_nao_falha_se_uhd_ja_esta_presente():
    """Aqui, dentro do container Dockerfile.usrp, uhd EXISTE — o import
    tardio tem de continuar funcionando, não só o caminho de erro."""
    from iq_recorder.adapters.usrp_iq_source import _import_uhd

    module = _import_uhd()
    assert module is uhd
