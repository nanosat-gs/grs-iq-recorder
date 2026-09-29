"""Arquivamento de raw packets: envelope, laço com banco instável, ZMQ e Postgres.

O teste do Postgres roda contra um banco DE VERDADE, num schema descartável
criado e apagado pelo próprio teste — nunca nas tabelas da estação. Sem
RECORDER_TEST_DATABASE_URL ele é pulado; no compose:

    docker compose run --rm -v "$PWD/repos/grs-iq-recorder/tests:/app/tests" \\
        grs-iq-recorder sh -c 'RECORDER_TEST_DATABASE_URL=$PG_DATABASE_URL pytest -q tests'
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import zmq

from iq_recorder.adapters.zmq_packet_source import ZmqPacketSource, parse_raw_packet
from iq_recorder.application.archive_packets import archive_packets
from iq_recorder.domain.models import RawPacket

NOW = datetime(2026, 9, 29, 12, 0, 0, 123456, tzinfo=timezone.utc)


def envelope(seq: int, payload: bytes = bytes(range(8)), **extra) -> list[bytes]:
    header = {"seq": seq, "bit_offset": 100 + 3200 * seq, "bits": len(payload) * 8,
              "bytes": len(payload), "max_sync_errors": 1, "syncword": "5DE62A7E",
              "bit_order": "msb_first", "detected_at": "2026-09-29T12:00:00Z", **extra}
    return [b"raw_packet", json.dumps(header).encode(), payload]


def packet(seq: int) -> RawPacket:
    return parse_raw_packet(envelope(seq), NOW)


# --- o envelope -------------------------------------------------------------------


def test_envelope_vira_rawpacket_com_os_campos_do_cabecalho():
    got = parse_raw_packet(envelope(7, payload=b"\x5d\xe6"), NOW)

    assert got.payload == b"\x5d\xe6"
    assert (got.detector_seq, got.bit_offset, got.max_sync_errors) == (7, 22500, 1)
    assert got.syncword == "5DE62A7E"
    assert got.detected_at == datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    assert got.received_at == NOW
    assert got.header["bit_order"] == "msb_first"


def test_campo_novo_do_detector_fica_no_cabecalho_guardado():
    """O detector pode passar a publicar algo que esta versão não conhece;
    o cabeçalho inteiro é guardado, então nada se perde."""
    got = parse_raw_packet(envelope(1, rssi_db=-97.5), NOW)

    assert got.header["rssi_db"] == -97.5


@pytest.mark.parametrize("frames", [
    [b"raw_packet", b"{}"],                          # 2 frames
    [b"outro", b"{}", b"x"],                         # tópico errado
    [b"raw_packet", b"nao-e-json", b"x"],
    [b"raw_packet", b"[1, 2]", b"x"],                # JSON que não é objeto
])
def test_envelope_malformado_e_recusado(frames):
    assert parse_raw_packet(frames, NOW) is None


def test_campos_de_tipo_errado_viram_none_sem_derrubar():
    frames = [b"raw_packet", json.dumps({"seq": "7", "detected_at": "ontem"}).encode(), b"x"]
    got = parse_raw_packet(frames, NOW)

    assert (got.detector_seq, got.detected_at) == (None, None)


# --- o laço de arquivamento ------------------------------------------------------


class FakeSource:
    def __init__(self, items, cancel_after=None, cancel=None):
        self._items = items
        self._cancel_after = cancel_after
        self._cancel = cancel

    def packets(self):
        for index, item in enumerate(self._items):
            # O pedido de parada chega JUNTO com o N-ésimo item, como um
            # SIGTERM que chega enquanto um pacote está sendo tratado.
            if self._cancel_after is not None and index + 1 >= self._cancel_after:
                self._cancel.set()
            yield item

    def close(self):
        pass


class FakeArchive:
    def __init__(self, failures: int = 0, always_fail: bool = False):
        self.batches: list[list[RawPacket]] = []
        self.attempts: list[list[int]] = []
        self._failures = failures
        self._always_fail = always_fail

    def append_many(self, packets):
        self.attempts.append([p.detector_seq for p in packets])
        if self._always_fail or self._failures > 0:
            self._failures -= 1
            raise ConnectionError("banco fora")
        self.batches.append(list(packets))

    def recent(self, limit=20):
        return []

    def close(self):
        pass


def seqs(archive: FakeArchive) -> list[int]:
    return [p.detector_seq for batch in archive.batches for p in batch]


def test_grava_em_lotes_pelo_tamanho():
    archive = FakeArchive()
    stats = archive_packets(FakeSource([packet(i) for i in range(7)]), archive,
                            threading.Event(), flush_size=3, flush_interval_s=1e9,
                            clock=itertools.count().__next__)

    assert [len(b) for b in archive.batches] == [3, 3, 1]
    assert seqs(archive) == list(range(7))
    assert (stats.received, stats.archived, stats.dropped) == (7, 7, 0)


def test_grava_pelo_tempo_mesmo_sem_pacote_novo():
    """Sem satélite no céu só chegam silêncios (None); o que está no buffer
    tem de ir para o banco assim mesmo, e não esperar o próximo pacote."""
    archive = FakeArchive()
    flushed_before_end: list[int] = []

    class Source:
        def packets(self):
            yield packet(0)
            yield None
            yield None
            yield None
            # Conferido ANTES da gravação final do encerramento, que cobriria
            # o caso mesmo sem a gravação por tempo.
            flushed_before_end.append(len(archive.batches))

        def close(self):
            pass

    clock = (x * 0.5 for x in itertools.count()).__next__
    archive_packets(Source(), archive, threading.Event(),
                    flush_size=100, flush_interval_s=2.0, clock=clock)

    assert flushed_before_end == [1]
    assert seqs(archive) == [0]


def test_banco_fora_mantem_o_lote_e_regrava_quando_volta():
    archive = FakeArchive(failures=2)
    items = [packet(i) for i in range(5)] + [None] * 30
    stats = archive_packets(FakeSource(items), archive, threading.Event(),
                            flush_size=5, flush_interval_s=1.0, retry_interval_s=5.0,
                            clock=itertools.count().__next__)

    assert seqs(archive) == [0, 1, 2, 3, 4]
    assert stats.failed_writes == 2
    assert (stats.archived, stats.dropped) == (5, 0)
    assert stats.last_error is None


def test_nao_martela_o_banco_enquanto_ele_esta_fora():
    archive = FakeArchive(always_fail=True)
    archive_packets(FakeSource([packet(0)] + [None] * 20), archive, threading.Event(),
                    flush_size=1, flush_interval_s=1.0, retry_interval_s=10.0,
                    clock=itertools.count().__next__)

    # 21 iterações com retry a cada 10: ~3 tentativas mais a final, não 21.
    assert len(archive.attempts) <= 4


def test_buffer_cheio_descarta_os_mais_antigos_e_conta():
    archive = FakeArchive(always_fail=True)
    stats = archive_packets(FakeSource([packet(i) for i in range(5)]), archive,
                            threading.Event(), flush_size=100, flush_interval_s=1e9,
                            max_buffer=3, clock=itertools.count().__next__)

    assert archive.attempts[-1] == [2, 3, 4]      # os mais novos sobreviveram
    assert stats.dropped == 5                     # 2 no buffer cheio + 3 ao encerrar
    assert stats.archived == 0


def test_parar_faz_uma_ultima_gravacao():
    cancel = threading.Event()
    archive = FakeArchive()
    stats = archive_packets(
        FakeSource([packet(0), packet(1), packet(2)], cancel_after=2, cancel=cancel),
        archive, cancel, flush_size=100, flush_interval_s=1e9,
        clock=itertools.count().__next__)

    assert seqs(archive) == [0, 1]
    assert stats.dropped == 0


# --- ZMQ de verdade -----------------------------------------------------------------


def test_fonte_zmq_entrega_pacotes_e_silencios():
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    port = publisher.bind_to_random_port("tcp://127.0.0.1")
    source = ZmqPacketSource(f"tcp://127.0.0.1:{port}", idle_timeout_ms=100)

    got: list = []
    deadline = time.monotonic() + 5
    for item in source.packets():
        if item is None and not any(isinstance(g, RawPacket) for g in got):
            publisher.send_multipart(envelope(3))       # reenvia até o SUB conectar
            publisher.send_multipart([b"raw_packet", b"lixo", b"x"])
        got.append(item)
        if (any(isinstance(g, RawPacket) for g in got) and got[-1] is None) \
                or time.monotonic() > deadline:
            break

    source.close()
    publisher.close(linger=0)
    context.term()

    packets = [g for g in got if isinstance(g, RawPacket)]
    assert packets and packets[0].detector_seq == 3
    assert None in got
    assert source.malformed >= 1


# --- Postgres de verdade -----------------------------------------------------------

DATABASE_URL = os.environ.get("RECORDER_TEST_DATABASE_URL")


@pytest.fixture
def pg_archive():
    if not DATABASE_URL:
        pytest.skip("RECORDER_TEST_DATABASE_URL não definido")
    from sqlalchemy import text

    from iq_recorder.adapters.postgres_packet_archive import PostgresPacketArchive

    schema = f"test_raw_packets_{uuid.uuid4().hex[:8]}"
    archive = PostgresPacketArchive(DATABASE_URL, schema=schema)
    yield archive
    with archive.engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    archive.close()


def test_postgres_cria_grava_e_le_de_volta(pg_archive):
    pg_archive.ensure_schema()
    pg_archive.ensure_schema()  # idempotente: roda a cada boot

    pg_archive.append_many([packet(0), packet(1)])
    pg_archive.append_many([packet(2)])

    rows = pg_archive.recent(10)
    assert pg_archive.count() == 3
    assert {r["detector_seq"] for r in rows} == {0, 1, 2}
    assert bytes(rows[0]["head"]) == bytes(range(8))
    assert rows[0]["received_at"] == NOW
    assert str(rows[0]["archiver_run"]) == str(pg_archive.run_id)


def test_postgres_guarda_payload_e_cabecalho_intactos(pg_archive):
    from sqlalchemy import text

    pg_archive.ensure_schema()
    original = parse_raw_packet(envelope(9, payload=bytes(range(255)), rssi_db=-97.5), NOW)
    pg_archive.append_many([original])

    with pg_archive.engine.connect() as connection:
        row = connection.execute(text(
            f"SELECT payload, header, payload_sha256 FROM {pg_archive.qualified}"
        )).one()

    assert bytes(row.payload) == bytes(range(255))
    assert row.header["rssi_db"] == -97.5 and row.header["seq"] == 9
    assert len(row.payload_sha256) == 64


def test_postgres_lote_com_erro_nao_grava_pela_metade(pg_archive):
    pg_archive.ensure_schema()
    ruim = RawPacket(received_at=None, payload=b"x", header={}, detector_seq=1,
                     bit_offset=None, detected_at=None, syncword=None, max_sync_errors=None)

    with pytest.raises(Exception):
        pg_archive.append_many([packet(0), ruim])   # received_at NOT NULL

    assert pg_archive.count() == 0


def test_schema_check_confere_a_tabela_nova(pg_archive):
    from iq_recorder.adapters.postgres_packet_archive import REQUIRED_COLUMNS, TABLE_NAME
    from iq_recorder.schema_check import check_table

    pg_archive.ensure_schema()

    assert check_table(pg_archive.engine, pg_archive.schema, TABLE_NAME,
                       REQUIRED_COLUMNS, "teste") is True
