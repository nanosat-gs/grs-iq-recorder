"""Adapter de saída: grava uma captura SigMF em disco (C2).

Implementa `IqSink`. Escreve o par que `docs/capture-contract.md` define:

    <nome>.sigmf-data   amostras, cruas
    <nome>.sigmf-meta   o descritor JSON

A REGRA QUE NÃO SE QUEBRA: o `.sigmf-data` recebe byte a byte o que veio do
socket. Sem normalizar, sem reordenar, sem cabeçalho. Não é purismo — se o
gravador transformasse as amostras na entrada, o replay republicaria algo que
o rádio nunca publicou, e o demodulador estaria sendo exercitado contra uma
ficção. A regressão que isso esconde é a pior que existe: a que só aparece com
hardware real, depois de o teste offline passar em verde.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timezone

from iq_recorder.domain.capture import DATA_SUFFIX, META_SUFFIX, build_sigmf_metadata
from iq_recorder.domain.models import CaptureMetadata, CaptureProfile, IqBlock


class FileIqSink:
    """Implementa IqSink escrevendo um par SigMF."""

    def __init__(self, directory: str | pathlib.Path, capture_id: str) -> None:
        if not capture_id or "/" in capture_id or "\\" in capture_id:
            # O id vira nome de arquivo. Uma barra aqui escreveria fora do
            # diretório de capturas, e um id vazio sobrescreveria `.sigmf-data`.
            raise ValueError(f"capture_id inválido para nome de arquivo: {capture_id!r}")

        self.directory = pathlib.Path(directory)
        self.capture_id = capture_id

        self.data_path = self.directory / f"{capture_id}{DATA_SUFFIX}"
        self.meta_path = self.directory / f"{capture_id}{META_SUFFIX}"

        self._profile: CaptureProfile | None = None
        self._handle = None
        self._digest = hashlib.sha512()
        self._bytes_written = 0
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None

    # --- IqSink -------------------------------------------------------------

    def open(self, profile: CaptureProfile) -> None:
        if self._handle is not None:
            raise RuntimeError("esta captura já está aberta")

        self.directory.mkdir(parents=True, exist_ok=True)

        if self.data_path.exists():
            # Nunca sobrescreve: uma captura é uma observação, e observação
            # perdida não se refaz. Mesmo argumento do índice append-only.
            raise FileExistsError(f"captura já existe: {self.data_path}")

        self._profile = profile
        self._handle = self.data_path.open("wb")

        # Sidecar já no início, SEM hash: um `.sigmf-meta` sem `core:sha512`
        # descreve uma captura em andamento — ou interrompida. Escrevê-lo agora
        # é o que faz uma gravação morta no meio deixar rastro legível em vez
        # de um arquivo de amostras órfão.
        self._write_metadata(sha512=None)

    def write(self, block: IqBlock) -> None:
        if self._handle is None:
            raise RuntimeError("write() antes de open()")

        if self._started_at is None:
            # O início é o primeiro lote que CHEGOU, não o instante do open():
            # entre abrir o arquivo e o rádio entregar o primeiro bloco pode
            # haver segundos, e datar a captura pelo open() deslocaria todas as
            # anotações de tempo dela.
            self._started_at = block.received_at

        self._handle.write(block.data)
        self._digest.update(block.data)
        self._bytes_written += len(block.data)
        self._ended_at = block.received_at

    def close(self) -> CaptureMetadata:
        if self._handle is None:
            raise RuntimeError("close() antes de open()")

        self._handle.close()
        self._handle = None

        assert self._profile is not None

        sha512 = self._digest.hexdigest()
        self._write_metadata(sha512=sha512)

        now = datetime.now(timezone.utc)
        started = self._started_at or now
        ended = self._ended_at or started

        return CaptureMetadata(
            capture_id=self.capture_id,
            profile_name=self._profile.name,
            center_frequency_hz=self._profile.center_frequency_hz,
            sample_rate_hz=self._profile.sample_rate_hz,
            datatype=self._profile.datatype,
            started_at=started,
            ended_at=ended,
            sample_count=self.sample_count,
            sha512=sha512,
            data_path=str(self.data_path),
        )

    # --- detalhes -----------------------------------------------------------

    @property
    def sample_count(self) -> int:
        """Amostras COMPLETAS gravadas.

        Divisão inteira: um resto significa que a origem entregou meia amostra,
        e isso é sintoma de envelope mal definido lá atrás — não algo que o
        gravador deva disfarçar arredondando para cima.
        """
        assert self._profile is not None

        return self._bytes_written // self._profile.datatype.bytes_per_sample

    @property
    def trailing_bytes(self) -> int:
        """Bytes que sobraram de uma amostra incompleta. Deve ser sempre 0."""
        assert self._profile is not None

        return self._bytes_written % self._profile.datatype.bytes_per_sample

    def _write_metadata(self, sha512: str | None) -> None:
        assert self._profile is not None

        metadata = build_sigmf_metadata(
            profile=self._profile,
            started_at=self._started_at or datetime.now(timezone.utc),
            sample_count=self.sample_count,
            sha512=sha512,
        )
        self.meta_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
