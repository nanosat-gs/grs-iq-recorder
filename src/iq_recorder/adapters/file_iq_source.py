"""Adapter de entrada: lê uma captura do disco (C3).

Implementa `IqStreamSource` — a MESMA porta do `ZmqIqSource`. É essa igualdade
que carrega o desenho todo: gravar de um rádio e reproduzir de um arquivo são
o mesmo laço com adapters diferentes, e é por isso que o teste ponta a ponta
roda sem hardware.

Lê o par SigMF e devolve os bytes exatamente como estão gravados. Como o
`FileIqSink` escreveu byte a byte o que veio do socket, o que sai daqui é
byte a byte o que o rádio publicou.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timezone
from typing import Iterator

from iq_recorder.domain.capture import DATA_SUFFIX, META_SUFFIX, GRS_NAMESPACE
from iq_recorder.domain.models import CaptureProfile, FrequencySource, IqBlock, SampleFormat


class CaptureIntegrityError(RuntimeError):
    """O arquivo não é o que o sidecar diz que é."""


class FileIqSource:
    """Implementa IqStreamSource lendo uma captura SigMF."""

    def __init__(
        self,
        path: str | pathlib.Path,
        block_samples: int = 8192,
        verify: bool = True,
    ) -> None:
        if block_samples <= 0:
            raise ValueError("block_samples precisa ser positivo")

        # Aceita o caminho com qualquer um dos dois sufixos, ou sem nenhum: na
        # prática as pessoas copiam o nome que virem, e recusar um dos dois
        # seria uma pegadinha sem propósito.
        base = pathlib.Path(path)
        if base.suffix in (DATA_SUFFIX, META_SUFFIX):
            base = base.with_suffix("")

        self.data_path = base.with_suffix(DATA_SUFFIX)
        self.meta_path = base.with_suffix(META_SUFFIX)

        if not self.data_path.exists():
            raise FileNotFoundError(f"amostras não encontradas: {self.data_path}")
        if not self.meta_path.exists():
            raise FileNotFoundError(f"sidecar não encontrado: {self.meta_path}")

        self.metadata = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.profile = self._profile_from_metadata()
        self.block_samples = block_samples

        if verify:
            self.verify()

    # --- IqStreamSource -----------------------------------------------------

    def blocks(self) -> Iterator[IqBlock]:
        """Lotes do arquivo, até acabar.

        Ao contrário do tap ao vivo, este fluxo TERMINA — um arquivo tem fim.
        Quem consome não precisa saber: o laço de replay simplesmente acaba.
        """
        chunk_bytes = self.block_samples * self.profile.datatype.bytes_per_sample
        sequence = 0

        with self.data_path.open("rb") as handle:
            while True:
                payload = handle.read(chunk_bytes)
                if not payload:
                    break

                # `received_at` é o instante do REPLAY, não o da gravação: é
                # quando este byte passou por aqui desta vez. O instante
                # original está no sidecar, que é onde ele tem de estar.
                yield IqBlock(
                    data=payload,
                    sequence=sequence,
                    received_at=datetime.now(timezone.utc),
                )
                sequence += 1

    def close(self) -> None:
        # Nada a fechar: cada `blocks()` abre e fecha o próprio handle. Existe
        # para satisfazer a porta, e para que trocar um adapter pelo outro não
        # exija mexer em quem chama.
        pass

    # --- integridade --------------------------------------------------------

    @property
    def sample_count(self) -> int:
        return self.data_path.stat().st_size // self.profile.datatype.bytes_per_sample

    def verify(self) -> None:
        """Confere o arquivo contra o que o sidecar afirma.

        Roda por padrão. Uma captura corrompida que passa despercebida vira
        horas caçando um bug de DSP que não existe — o custo de ler o arquivo
        uma vez a mais é barato perto disso.
        """
        declared = self.metadata.get("global", {}).get("core:sha512")

        if declared is None:
            # Sem hash = captura em andamento ou interrompida. Não é erro:
            # reproduzir o que foi salvo antes de uma queda é um uso legítimo.
            return

        digest = hashlib.sha512()
        with self.data_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)

        if digest.hexdigest() != declared:
            raise CaptureIntegrityError(
                f"{self.data_path.name}: sha512 não confere com o sidecar. "
                "O arquivo mudou depois de gravado, ou a cópia veio truncada."
            )

    def _profile_from_metadata(self) -> CaptureProfile:
        """Reconstrói o CaptureProfile a partir do sidecar.

        Do ARQUIVO, e não do registro de perfis: uma captura de seis meses
        atrás foi feita com a configuração daquele dia, e relê-la com o perfil
        de hoje reescreveria a história. É por isso que o contrato exige que a
        captura seja auto-descritiva.
        """
        global_block = self.metadata.get("global", {})
        captures = self.metadata.get("captures") or [{}]

        datatype = global_block.get("core:datatype")
        if datatype != SampleFormat.CF32_LE.value:
            raise CaptureIntegrityError(
                f"datatype não suportado: {datatype!r} (esperado cf32_le)"
            )

        source = global_block.get(f"{GRS_NAMESPACE}:frequency_source", FrequencySource.FIXED.value)

        return CaptureProfile(
            name=global_block.get(f"{GRS_NAMESPACE}:profile", "captura"),
            description=global_block.get("core:description", ""),
            center_frequency_hz=float(captures[0].get("core:frequency", 0.0) or 0.0),
            sample_rate_hz=float(global_block.get("core:sample_rate", 0.0) or 0.0),
            datatype=SampleFormat.CF32_LE,
            gain_db=global_block.get(f"{GRS_NAMESPACE}:gain_db"),
            rf_path=global_block.get(f"{GRS_NAMESPACE}:rf_path", ""),
            frequency_source=FrequencySource(source),
        )
