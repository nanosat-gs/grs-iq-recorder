"""O contador de raw packets (--count-packets), contra um PUB de verdade."""

from __future__ import annotations

import json
import threading
import time

import zmq

from iq_recorder.adapters.zmq_packet_counter import PacketCounter
from iq_recorder.domain.profiles import GRS_RX_FS2


def raw_packet(seq: int, bit_offset: int) -> list[bytes]:
    header = json.dumps({"seq": seq, "bit_offset": bit_offset}).encode()
    return [b"raw_packet", header, bytes(range(16))]


def test_conta_os_pacotes_e_o_espacamento():
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    port = publisher.bind_to_random_port("tcp://127.0.0.1")

    counter = PacketCounter(f"tcp://127.0.0.1:{port}", grace_s=0.3)
    counter.start()
    for seq in range(5):
        publisher.send_multipart(raw_packet(seq, 1000 + 3200 * seq))
    publisher.send_multipart([b"outro_topico", b"{}", b""])  # não conta
    publisher.send_multipart([b"raw_packet", b"{}"])          # envelope errado: não conta

    count = counter.finish()
    publisher.close(linger=0)
    context.term()

    assert count == 5
    assert counter.bit_offsets == [1000 + 3200 * s for s in range(5)]


def test_espera_os_pacotes_que_chegam_depois_do_fim():
    """O último pacote de um replay sai do detector depois do último lote de
    IQ — a contagem não pode fechar antes dele."""
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    port = publisher.bind_to_random_port("tcp://127.0.0.1")
    counter = PacketCounter(f"tcp://127.0.0.1:{port}", grace_s=0.6)
    counter.start()

    late = threading.Timer(0.3, lambda: publisher.send_multipart(raw_packet(0, 0)))
    late.start()
    started = time.monotonic()
    count = counter.finish()
    late.join()
    publisher.close(linger=0)
    context.term()

    assert count == 1
    assert time.monotonic() - started >= 0.6


def test_detector_ausente_da_zero_sem_travar():
    counter = PacketCounter("tcp://127.0.0.1:1", grace_s=0.1)
    counter.start()

    assert counter.finish() == 0


def test_perfil_traz_o_syncword_certo():
    """BA 67 54 7E (bits invertidos) circulou no documento da fatia e foi
    parar na descrição do perfil — e dali no sidecar de cada captura."""
    assert "5D E6 2A 7E" in GRS_RX_FS2.description
    assert "BA 67" not in GRS_RX_FS2.description
