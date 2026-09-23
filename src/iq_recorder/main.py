"""Ponto de entrada do GRS IQ Recorder.

Dois subcomandos, e a simetria entre eles é o serviço inteiro:

    record   ZmqIqSource  -> FileIqSink      -> PostgresCaptureIndex
    replay   FileIqSource -> ZmqIqPublisher

A porta da ESQUERDA é a mesma nos dois. Gravar de um rádio e reproduzir de um
arquivo são o mesmo laço com adapters diferentes — e é por isso que o teste
ponta a ponta roda sem hardware.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from types import FrameType

from iq_recorder.config import RecorderConfig, load_config
from iq_recorder.domain.capture import CAPTURE_CONTRACT_VERSION, SIGMF_VERSION
from iq_recorder.domain.models import CaptureProfile, FrequencySource

logger = logging.getLogger("iq_recorder")

_cancel = threading.Event()


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    logger.info("Sinal %d recebido, encerrando.", signum)
    _cancel.set()


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def print_boot_summary(config: RecorderConfig) -> None:
    """Resumo do boot: o que este processo gravaria, e como."""
    profile = config.profile

    logger.info("perfil ............ %s", profile.name)
    logger.info("descrição ......... %s", profile.description)
    logger.info("frequência ........ %.4f MHz", profile.center_frequency_hz / 1e6)
    logger.info("taxa .............. %.1f kS/s", profile.sample_rate_hz / 1e3)
    logger.info("formato ........... %s", profile.datatype.value)
    logger.info("ganho ............. %s",
                "automático" if profile.gain_db is None else f"{profile.gain_db} dB")
    logger.info("vazão ............. %.2f MB/s", profile.bytes_per_second / 1e6)
    logger.info("contrato .......... SigMF %s / perfil GRS %s",
                SIGMF_VERSION, CAPTURE_CONTRACT_VERSION)

    if profile.frequency_source is FrequencySource.FIXED:
        logger.warning(
            "Frequência DECLARADA, não observada — sintonia fixa, sem Doppler. O "
            "sidecar diz o que o perfil mandou sintonizar, não o que o rádio de "
            "fato sintonizou."
        )


def build_index(config: RecorderConfig):
    """Devolve o índice, ou None se não houver banco configurado.

    Sem `PG_DATABASE_URL` o gravador grava do mesmo jeito: a captura é o
    artefato, o índice é conveniência. Mesmo argumento do painel do GRS
    Manager, que degrada sem o TC Scheduler em vez de falhar.
    """
    if config.database_url is None:
        logger.warning("Sem PG_DATABASE_URL — as capturas não serão indexadas.")
        return None

    # Import tardio: sem banco configurado, não há por que pagar o custo de
    # carregar SQLAlchemy nem de tentar resolver o driver.
    from iq_recorder.adapters.postgres_capture_index import PostgresCaptureIndex
    from iq_recorder.schema_check import check_schema

    try:
        index = PostgresCaptureIndex(config.database_url)
        index.ensure_schema()
        check_schema(index._engine)
        return index
    except Exception:
        logger.exception("Índice indisponível. A gravação segue sem indexar.")
        return None


def default_capture_id(profile: CaptureProfile) -> str:
    """Nome legível e ordenável: perfil + instante UTC.

    Ordenável importa mais do que parece — `ls` num diretório de capturas passa
    a listar em ordem cronológica sem nenhuma ferramenta.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return f"{profile.name}-{stamp}"


def do_record(args: argparse.Namespace, config: RecorderConfig) -> int:
    from iq_recorder.adapters.file_iq_sink import FileIqSink
    from iq_recorder.adapters.zmq_iq_source import ZmqIqSource
    from iq_recorder.application.record import record

    capture_id = args.capture_id or default_capture_id(config.profile)
    seconds = args.seconds if args.seconds is not None else config.max_capture_seconds

    logger.info("Gravando %s por até %d s, de %s.",
                capture_id, seconds, config.iq_source_address)
    logger.info("Isso são até %.0f MB.", config.profile.bytes_per_second * seconds / 1e6)

    source = ZmqIqSource(config.iq_source_address)
    sink = FileIqSink(config.capture_dir, capture_id)
    index = build_index(config)

    try:
        metadata = record(
            source=source,
            sink=sink,
            profile=config.profile,
            max_seconds=seconds,
            max_bytes=args.max_bytes,
            index=index,
            cancel=_cancel,
        )
    finally:
        source.close()
        if index is not None:
            index.close()

    if metadata.sample_count == 0:
        logger.error(
            "Nenhuma amostra gravada. Há alguém publicando em %s? No profile "
            "rxsim quem publica é o grs-sdr-sim; no rx, o grs-iq-rx.",
            config.iq_source_address,
        )
        return 1

    logger.info("%s: %d amostras, %.1f s, sha512 %s…",
                metadata.capture_id, metadata.sample_count,
                metadata.duration_seconds, metadata.sha512[:16])
    logger.info("Arquivo: %s", metadata.data_path)

    return 0


def do_replay(args: argparse.Namespace, config: RecorderConfig) -> int:
    from iq_recorder.adapters.file_iq_source import FileIqSource
    from iq_recorder.adapters.zmq_iq_publisher import ZmqIqPublisher
    from iq_recorder.application.replay import replay

    source = FileIqSource(args.capture, block_samples=args.block_samples)

    logger.info("Reproduzindo %s: %d amostras, %.1f s de sinal.",
                source.data_path.name, source.sample_count,
                source.sample_count / source.profile.sample_rate_hz)
    logger.warning(
        "O publicador BINDA %s. O grs-iq-rx (ou o grs-sdr-sim) tem de estar "
        "DESLIGADO — os dois disputam a porta, e quem perde cai em silêncio.",
        config.iq_publish_address,
    )

    publisher = ZmqIqPublisher(config.iq_publish_address)

    try:
        blocks = replay(
            source=source,
            publisher=publisher,
            # O perfil vem do ARQUIVO, não do registro: a captura foi feita com
            # a configuração daquele dia.
            profile=source.profile,
            realtime=not args.fast,
            cancel=_cancel,
        )
    finally:
        publisher.close()

    logger.info("%d lotes republicados.", blocks)

    return 0


def do_inspect(args: argparse.Namespace, config: RecorderConfig) -> int:
    """A leitura mínima: tem sinal nesta captura, e onde? (D1)"""
    from iq_recorder.adapters.file_iq_source import FileIqSource
    from iq_recorder.adapters.numpy_psd_view import NumpyPsdView

    source = FileIqSource(args.capture)
    view = NumpyPsdView()
    summary = view.summarize(source, source.profile, fft_size=args.fft_size)

    logger.info("captura ........... %s", source.data_path.name)
    logger.info("perfil ............ %s", source.profile.name)
    logger.info("amostras .......... %d", summary.sample_count)
    logger.info("duração ........... %.3f s", summary.duration_seconds)
    logger.info("sintonia .......... %.4f MHz", summary.center_frequency_hz / 1e6)
    logger.info("taxa .............. %.1f kS/s", summary.sample_rate_hz / 1e3)
    logger.info("pico do espectro .. %+.2f kHz do centro", summary.peak_offset_hz / 1e3)
    logger.info("pico acima do piso  %.1f dB", summary.peak_above_floor_db)

    if summary.has_signal:
        logger.info("Há sinal: o pico se levanta do piso.")
    else:
        logger.warning(
            "Isto parece RUÍDO: o maior bin está a %.1f dB do piso. Antes de "
            "procurar defeito no demodulador, confira a sintonia e a antena.",
            summary.peak_above_floor_db,
        )

    if args.csv:
        freqs, psd_db = view.power_spectral_density(
            FileIqSource(args.capture, verify=False), source.profile, fft_size=args.fft_size
        )
        with open(args.csv, "w", encoding="utf-8") as handle:
            handle.write("offset_hz,db\n")
            for frequency, power in zip(freqs, psd_db):
                handle.write(f"{frequency:.1f},{power:.3f}\n")
        logger.info("Espectro em %s", args.csv)

    return 0 if summary.has_signal else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iq-recorder", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="grava uma captura do PUB de IQ")
    rec.add_argument("--capture-id", default=None,
                     help="Nome da captura. Padrão: perfil + instante UTC.")
    rec.add_argument("--seconds", type=int, default=None,
                     help="Teto de tempo. Padrão: RECORDER_MAX_CAPTURE_SECONDS.")
    rec.add_argument("--max-bytes", type=int, default=None,
                     help="Teto de disco, em bytes. Vale junto com --seconds: o "
                          "primeiro que bater encerra. Protege contra uma taxa maior "
                          "que a esperada, que nenhum teto de tempo pega — e é o "
                          "controle certo para gerar uma fixture de tamanho exato.")

    rep = sub.add_parser("replay", help="republica uma captura no tópico de IQ")
    rep.add_argument("capture", help="Caminho da captura, com ou sem sufixo.")
    rep.add_argument("--block-samples", type=int, default=8192,
                     help="Amostras por mensagem ZMQ.")
    rep.add_argument("--fast", action="store_true",
                     help="Despeja sem ritmo. Só para consumidor offline: a toda "
                          "velocidade a marca d'água do demodulador estoura e o ZMQ "
                          "começa a DESCARTAR blocos.")

    ins = sub.add_parser("inspect", help="resumo e espectro de uma captura")
    ins.add_argument("capture", help="Caminho da captura, com ou sem sufixo.")
    ins.add_argument("--fft-size", type=int, default=4096)
    ins.add_argument("--csv", default=None,
                     help="Escreve o espectro num CSV, para plotar fora daqui.")

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except (KeyError, ValueError) as error:
        # Configuração ruim derruba o boot, com a mensagem inteira. O modo de
        # falha que isso evita: subir com o perfil padrão por causa de um erro
        # de digitação no .env, e gravar uma campanha no enlace errado.
        logger.error("Configuração inválida: %s", error)
        return 1

    print_boot_summary(config)
    install_signal_handlers()

    if args.command == "record":
        return do_record(args, config)
    if args.command == "replay":
        return do_replay(args, config)
    if args.command == "inspect":
        return do_inspect(args, config)

    # Sem subcomando: fica de pé. É o que o serviço faz no compose, onde ele
    # sobe junto com a estação e espera um comando — gravar é sob demanda, e a
    # gravação disparada por passagem (AOS/LOS) está fora desta fatia.
    logger.info("De pé, sem gravar. Use `record` ou `replay`.")
    _cancel.wait()
    logger.info("Encerrado.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
