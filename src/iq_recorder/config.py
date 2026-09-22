"""Configuração do GRS IQ Recorder, lida do ambiente.

Fronteira firme com domain/profiles.py: aqui fica o que muda de MÁQUINA para
máquina (endereço do socket, diretório de captura, limite de gravação); lá
fica o que descreve o ENLACE. Frequência e taxa de amostragem não aparecem
neste arquivo, e isso é deliberado — se dessem para sobrescrever pelo .env, a
captura passaria a depender de qual era o .env naquele dia, que é exatamente
o que o CaptureProfile existe para evitar.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from iq_recorder.domain.models import CaptureProfile
from iq_recorder.domain.profiles import get_profile

DEFAULT_PROFILE = "grs-rx-fs2"
DEFAULT_IQ_SOURCE_ADDRESS = "tcp://grs-iq-rx:5556"
DEFAULT_IQ_PUBLISH_ADDRESS = "tcp://*:5556"
DEFAULT_CAPTURE_DIR = "/app/captures"
DEFAULT_MAX_CAPTURE_SECONDS = 60


@dataclass(frozen=True)
class RecorderConfig:
    profile: CaptureProfile
    iq_source_address: str
    iq_publish_address: str
    capture_dir: str
    max_capture_seconds: int
    database_url: str | None


def _int_from(source: dict[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} precisa ser inteiro, veio {raw!r}") from None


def load_config(env: dict[str, str] | None = None) -> RecorderConfig:
    """Resolve a configuração e valida tudo que dá para validar no boot.

    Erro aqui derruba o serviço, e é o que se quer: um perfil inexistente ou
    um limite de gravação absurdo tem de aparecer no `docker compose up`, não
    no meio de uma passagem.

    :param env: ambiente a ler. None usa os.environ; um dicionário explícito é
        o que os testes passam, sem mexer no ambiente do processo.
    """
    source = dict(os.environ) if env is None else env

    max_seconds = _int_from(source, "RECORDER_MAX_CAPTURE_SECONDS", DEFAULT_MAX_CAPTURE_SECONDS)
    if max_seconds <= 0:
        raise ValueError(f"RECORDER_MAX_CAPTURE_SECONDS precisa ser positivo, veio {max_seconds}")

    return RecorderConfig(
        # get_profile explode com a lista dos perfis conhecidos se o nome não
        # existir — ver domain/profiles.py.
        profile=get_profile(source.get("RECORDER_PROFILE", DEFAULT_PROFILE)),
        iq_source_address=source.get("RECORDER_IQ_SOURCE_ADDRESS", DEFAULT_IQ_SOURCE_ADDRESS),
        iq_publish_address=source.get("RECORDER_IQ_PUBLISH_ADDRESS", DEFAULT_IQ_PUBLISH_ADDRESS),
        capture_dir=source.get("RECORDER_CAPTURE_DIR", DEFAULT_CAPTURE_DIR),
        max_capture_seconds=max_seconds,
        # None = sem índice. O gravador tem de funcionar sem Postgres: a
        # captura é o artefato, o índice é conveniência. Mesmo argumento do
        # painel do GRS Manager, que degrada sem o TC Scheduler em vez de
        # falhar.
        database_url=source.get("PG_DATABASE_URL") or None,
    )
