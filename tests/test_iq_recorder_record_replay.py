"""Testes do Épico C: captura, replay e a simetria entre os dois.

O teste central é o round-trip: gravar e reproduzir tem de devolver os MESMOS
bytes. É a regra que sustenta todo o resto — se o gravador transformasse as
amostras na entrada, o replay republicaria um sinal que o rádio nunca produziu,
e o demodulador estaria sendo exercitado contra uma ficção.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from iq_recorder.adapters.file_iq_sink import FileIqSink
from iq_recorder.adapters.file_iq_source import CaptureIntegrityError, FileIqSource
from iq_recorder.application.record import record
from iq_recorder.application.replay import replay
from iq_recorder.domain.models import IqBlock, SampleFormat
from iq_recorder.domain.profiles import GRS_RX_FS2

START = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def make_samples(count: int, seed: int = 3) -> bytes:
    """IQ sintético em cf32_le — o formato que o contrato exige."""
    rng = np.random.default_rng(seed)
    values = rng.normal(size=count) + 1j * rng.normal(size=count)

    return values.astype(np.complex64).tobytes()


class ListSource:
    """IqStreamSource em memória, para exercitar o laço sem socket."""

    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads
        self.closed = False

    def blocks(self):
        for index, payload in enumerate(self.payloads):
            yield IqBlock(
                data=payload,
                sequence=index,
                received_at=START + timedelta(seconds=index),
            )

    def close(self) -> None:
        self.closed = True


class ListPublisher:
    def __init__(self) -> None:
        self.published: list[bytes] = []
        self.closed = False

    def publish(self, block: IqBlock) -> None:
        self.published.append(block.data)

    def close(self) -> None:
        self.closed = True


# --- gravação ---------------------------------------------------------------


def test_grava_o_par_sigmf(tmp_path):
    source = ListSource([make_samples(1000), make_samples(1000, seed=4)])
    sink = FileIqSink(tmp_path, "captura-1")

    metadata = record(source, sink, GRS_RX_FS2)

    assert sink.data_path.exists()
    assert sink.meta_path.exists()
    assert metadata.sample_count == 2000


def test_os_bytes_gravados_sao_os_bytes_recebidos(tmp_path):
    """A regra que não se quebra."""
    payloads = [make_samples(500), make_samples(500, seed=9)]
    sink = FileIqSink(tmp_path, "captura-crua")

    record(ListSource(payloads), sink, GRS_RX_FS2)

    assert sink.data_path.read_bytes() == b"".join(payloads)


def test_hash_do_sidecar_e_do_arquivo_de_amostras(tmp_path):
    payloads = [make_samples(800)]
    sink = FileIqSink(tmp_path, "captura-hash")

    metadata = record(ListSource(payloads), sink, GRS_RX_FS2)

    esperado = hashlib.sha512(b"".join(payloads)).hexdigest()
    assert metadata.sha512 == esperado
    assert json.loads(sink.meta_path.read_text())["global"]["core:sha512"] == esperado


def test_sidecar_nasce_sem_hash_e_ganha_um_ao_fechar(tmp_path):
    """Sidecar sem core:sha512 descreve captura em andamento — ou interrompida.
    Escrevê-lo já no open() é o que faz uma gravação morta no meio deixar
    rastro legível em vez de um arquivo de amostras órfão."""
    sink = FileIqSink(tmp_path, "captura-aberta")
    sink.open(GRS_RX_FS2)

    assert "core:sha512" not in json.loads(sink.meta_path.read_text())["global"]

    sink.write(IqBlock(make_samples(100), 0, START))
    sink.close()

    assert "core:sha512" in json.loads(sink.meta_path.read_text())["global"]


def test_inicio_e_o_primeiro_lote_e_nao_o_open(tmp_path):
    """Entre abrir o arquivo e o rádio entregar o primeiro bloco pode haver
    segundos; datar pelo open() deslocaria todas as anotações de tempo."""
    sink = FileIqSink(tmp_path, "captura-tempo")

    metadata = record(ListSource([make_samples(10), make_samples(10)]), sink, GRS_RX_FS2)

    assert metadata.started_at == START
    assert metadata.ended_at == START + timedelta(seconds=1)


def test_nao_sobrescreve_captura_existente(tmp_path):
    """Uma captura é uma observação, e observação perdida não se refaz."""
    record(ListSource([make_samples(10)]), FileIqSink(tmp_path, "unica"), GRS_RX_FS2)

    with pytest.raises(FileExistsError):
        FileIqSink(tmp_path, "unica").open(GRS_RX_FS2)


def test_id_com_barra_e_recusado(tmp_path):
    """O id vira nome de arquivo; uma barra escreveria fora do diretório."""
    with pytest.raises(ValueError, match="capture_id"):
        FileIqSink(tmp_path, "../fuga")


def test_teto_de_tempo_para_a_gravacao(tmp_path):
    relogio = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    source = ListSource([make_samples(10) for _ in range(6)])

    metadata = record(
        source, FileIqSink(tmp_path, "c-tempo"), GRS_RX_FS2,
        max_seconds=2.0, clock=lambda: next(relogio),
    )

    assert metadata.sample_count < 60


def test_teto_de_disco_para_a_gravacao(tmp_path):
    """Protege contra uma taxa maior que a esperada, que nenhum teto de tempo
    pega."""
    source = ListSource([make_samples(100) for _ in range(10)])

    metadata = record(
        source, FileIqSink(tmp_path, "c-disco"), GRS_RX_FS2, max_bytes=800 * 3
    )

    assert metadata.sample_count == 300


def test_cancelamento_externo_para_a_gravacao(tmp_path):
    cancel = threading.Event()
    cancel.set()

    metadata = record(
        ListSource([make_samples(10) for _ in range(5)]),
        FileIqSink(tmp_path, "c-cancel"), GRS_RX_FS2, cancel=cancel,
    )

    assert metadata.sample_count == 10


def test_captura_fecha_mesmo_se_a_origem_explodir(tmp_path):
    """Uma captura interrompida ainda é uma captura."""

    class Exploding(ListSource):
        def blocks(self):
            yield IqBlock(make_samples(50), 0, START)
            raise RuntimeError("rádio caiu")

    sink = FileIqSink(tmp_path, "c-explode")

    with pytest.raises(RuntimeError):
        record(Exploding([]), sink, GRS_RX_FS2)

    assert sink.data_path.exists()
    assert sink.data_path.stat().st_size == 400


def test_indice_que_falha_nao_derruba_a_captura(tmp_path):
    """O índice é conveniência; a captura é o artefato."""

    class BrokenIndex:
        def append(self, metadata):
            raise RuntimeError("banco fora do ar")

        def list_captures(self, limit=100):
            return []

        def get(self, capture_id):
            return None

    metadata = record(
        ListSource([make_samples(10)]), FileIqSink(tmp_path, "c-indice"),
        GRS_RX_FS2, index=BrokenIndex(),
    )

    assert metadata.sample_count == 10


# --- leitura ----------------------------------------------------------------


def test_le_a_captura_de_volta(tmp_path):
    payloads = [make_samples(1000)]
    record(ListSource(payloads), FileIqSink(tmp_path, "c-leitura"), GRS_RX_FS2)

    source = FileIqSource(tmp_path / "c-leitura.sigmf-data")

    assert source.sample_count == 1000
    assert b"".join(b.data for b in source.blocks()) == payloads[0]


def test_perfil_vem_do_arquivo_e_nao_do_registro(tmp_path):
    """Uma captura de seis meses atrás foi feita com a configuração daquele
    dia; relê-la com o perfil de hoje reescreveria a história."""
    record(ListSource([make_samples(10)]), FileIqSink(tmp_path, "c-perfil"), GRS_RX_FS2)

    lido = FileIqSource(tmp_path / "c-perfil").profile

    assert lido.center_frequency_hz == GRS_RX_FS2.center_frequency_hz
    assert lido.sample_rate_hz == GRS_RX_FS2.sample_rate_hz
    assert lido.datatype is SampleFormat.CF32_LE


def test_aceita_o_caminho_com_qualquer_sufixo(tmp_path):
    record(ListSource([make_samples(10)]), FileIqSink(tmp_path, "c-sufixo"), GRS_RX_FS2)

    for caminho in ("c-sufixo", "c-sufixo.sigmf-data", "c-sufixo.sigmf-meta"):
        assert FileIqSource(tmp_path / caminho).sample_count == 10


def test_arquivo_corrompido_e_recusado(tmp_path):
    """Uma captura corrompida que passa despercebida vira horas caçando um bug
    de DSP que não existe."""
    record(ListSource([make_samples(100)]), FileIqSink(tmp_path, "c-corrompe"), GRS_RX_FS2)

    data = tmp_path / "c-corrompe.sigmf-data"
    corrompido = bytearray(data.read_bytes())
    corrompido[10] ^= 0xFF
    data.write_bytes(bytes(corrompido))

    with pytest.raises(CaptureIntegrityError, match="sha512"):
        FileIqSource(data)


def test_captura_sem_hash_pode_ser_lida(tmp_path):
    """Reproduzir o que foi salvo antes de uma queda é uso legítimo."""
    sink = FileIqSink(tmp_path, "c-sem-hash")
    sink.open(GRS_RX_FS2)
    sink.write(IqBlock(make_samples(50), 0, START))
    # Fecha o arquivo sem passar pelo close(), como numa queda.
    sink._handle.close()
    sink._handle = None

    assert FileIqSource(tmp_path / "c-sem-hash").sample_count == 50


def test_sidecar_ausente_e_erro_claro(tmp_path):
    record(ListSource([make_samples(10)]), FileIqSink(tmp_path, "c-orfa"), GRS_RX_FS2)
    (tmp_path / "c-orfa.sigmf-meta").unlink()

    with pytest.raises(FileNotFoundError, match="sidecar"):
        FileIqSource(tmp_path / "c-orfa")


# --- replay e round-trip ----------------------------------------------------


def test_round_trip_byte_a_byte(tmp_path):
    """O critério de pronto da fatia: gravar e reproduzir devolve os MESMOS
    bytes."""
    original = [make_samples(700), make_samples(700, seed=11), make_samples(300, seed=12)]

    record(ListSource(original), FileIqSink(tmp_path, "c-round"), GRS_RX_FS2)

    publisher = ListPublisher()
    replay(FileIqSource(tmp_path / "c-round"), publisher, GRS_RX_FS2, realtime=False)

    assert b"".join(publisher.published) == b"".join(original)


def test_round_trip_preserva_os_metadados_campo_a_campo(tmp_path):
    record(ListSource([make_samples(256)]), FileIqSink(tmp_path, "c-meta"), GRS_RX_FS2)

    lido = FileIqSource(tmp_path / "c-meta").profile

    assert lido.name == GRS_RX_FS2.name
    assert lido.description == GRS_RX_FS2.description
    assert lido.rf_path == GRS_RX_FS2.rf_path
    assert lido.gain_db == GRS_RX_FS2.gain_db
    assert lido.frequency_source is GRS_RX_FS2.frequency_source


def test_replay_reagrupa_os_lotes_sem_perder_amostra(tmp_path):
    """O tamanho de lote do replay não precisa bater com o da gravação — o que
    tem de bater é o fluxo de bytes."""
    original = [make_samples(1000)]
    record(ListSource(original), FileIqSink(tmp_path, "c-lotes"), GRS_RX_FS2)

    publisher = ListPublisher()
    source = FileIqSource(tmp_path / "c-lotes", block_samples=137)
    blocos = replay(source, publisher, GRS_RX_FS2, realtime=False)

    assert blocos == 8  # 1000 / 137, arredondado para cima
    assert b"".join(publisher.published) == original[0]


def test_replay_em_tempo_real_ritma_pela_taxa_do_perfil(tmp_path):
    """Despejar o arquivo a toda velocidade estoura a marca d'água do
    demodulador, e o ZMQ começa a descartar."""
    record(ListSource([make_samples(24_000)]), FileIqSink(tmp_path, "c-ritmo"), GRS_RX_FS2)

    dormidas: list[float] = []
    agora = [0.0]

    def clock():
        return agora[0]

    def sleep(seconds):
        dormidas.append(seconds)
        agora[0] += seconds

    replay(
        FileIqSource(tmp_path / "c-ritmo", block_samples=8192),
        ListPublisher(), GRS_RX_FS2, realtime=True, clock=clock, sleep=sleep,
    )

    # 24000 amostras a 240 kS/s = 0,1 s de sinal.
    assert sum(dormidas) == pytest.approx(0.1, rel=0.01)


def test_replay_sem_tempo_real_nao_dorme(tmp_path):
    record(ListSource([make_samples(16_000)]), FileIqSink(tmp_path, "c-rapido"), GRS_RX_FS2)

    dormidas: list[float] = []
    replay(
        FileIqSource(tmp_path / "c-rapido"), ListPublisher(), GRS_RX_FS2,
        realtime=False, sleep=dormidas.append,
    )

    assert dormidas == []


def test_replay_fecha_a_origem(tmp_path):
    source = ListSource([make_samples(10)])

    replay(source, ListPublisher(), GRS_RX_FS2, realtime=False)

    assert source.closed
