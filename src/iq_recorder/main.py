"""Ponto de entrada do GRS IQ Recorder.

ESTADO: esqueleto (A2). O serviço resolve a configuração, confere o perfil de
captura, imprime o resumo do que gravaria e fica de pé. Ele ainda NÃO grava:
o tap ao vivo é o C1, o FileIqSink é o C2, o replay é o C3 e o índice é o C4.

Ficar de pé sem fazer nada é deliberado, e é o que o A2 pede: prova que o
serviço entra no compose, sobe saudável e cai isolado com SIGTERM, sem que
nenhuma decisão de gravação tenha sido tomada ainda. O resumo impresso no
boot é o que torna isso verificável de fora — sem ele, "subiu" e "subiu com o
perfil errado" teriam a mesma aparência.
"""

from __future__ import annotations

import signal
import sys
import threading
from types import FrameType

from iq_recorder.config import RecorderConfig, load_config
from iq_recorder.domain.capture import CAPTURE_CONTRACT_VERSION, SIGMF_VERSION
from iq_recorder.domain.models import FrequencySource

_shutdown = threading.Event()


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    print(f"[iq-recorder] sinal {signum} recebido, encerrando", flush=True)
    _shutdown.set()


def print_boot_summary(config: RecorderConfig) -> None:
    """Resumo do boot: o que este processo gravaria, se já gravasse."""
    profile = config.profile
    print("[iq-recorder] GRS IQ Recorder — esqueleto (A2), ainda não grava", flush=True)
    print(f"[iq-recorder] perfil ............ {profile.name}", flush=True)
    print(f"[iq-recorder] descrição ......... {profile.description}", flush=True)
    print(f"[iq-recorder] frequência ........ {profile.center_frequency_hz / 1e6:.4f} MHz", flush=True)
    print(f"[iq-recorder] taxa .............. {profile.sample_rate_hz / 1e3:.1f} kS/s", flush=True)
    print(f"[iq-recorder] formato ........... {profile.datatype.value}", flush=True)
    print(f"[iq-recorder] ganho ............. {'automático' if profile.gain_db is None else f'{profile.gain_db} dB'}", flush=True)
    print(f"[iq-recorder] vazão ............. {profile.bytes_per_second / 1e6:.2f} MB/s", flush=True)
    print(f"[iq-recorder] fonte de IQ ....... {config.iq_source_address}", flush=True)
    print(f"[iq-recorder] capturas em ....... {config.capture_dir}", flush=True)
    print(f"[iq-recorder] contrato .......... SigMF {SIGMF_VERSION} / perfil GRS {CAPTURE_CONTRACT_VERSION}", flush=True)

    if profile.frequency_source is FrequencySource.FIXED:
        print(
            "[iq-recorder] AVISO: frequência DECLARADA, não observada — sintonia fixa, "
            "sem Doppler. O sidecar diz o que o perfil mandou sintonizar, não o que o "
            "rádio de fato sintonizou.",
            flush=True,
        )

    if config.database_url is None:
        print(
            "[iq-recorder] AVISO: sem PG_DATABASE_URL — as capturas não serão indexadas. "
            "O arquivo continua sendo o artefato; o índice é conveniência.",
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    """:return: código de saída do processo."""
    del argv  # Sem argumentos por enquanto: tudo vem do ambiente.

    try:
        config = load_config()
    except (KeyError, ValueError) as error:
        # Configuração ruim derruba o boot, com a mensagem inteira. O modo de
        # falha que isso evita: subir com o perfil padrão porque o nome no
        # .env estava com erro de digitação, e gravar uma campanha inteira no
        # enlace errado.
        print(f"[iq-recorder] configuração inválida: {error}", file=sys.stderr, flush=True)
        return 1

    print_boot_summary(config)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print("[iq-recorder] de pé; aguardando os laços de gravação (C1..C4)", flush=True)
    _shutdown.wait()
    print("[iq-recorder] encerrado", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
