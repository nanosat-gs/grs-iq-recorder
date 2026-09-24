"""Adapter de entrada: um WAV do gqrx vira fluxo de IQ.

Implementa `IqStreamSource` — a MESMA porta do tap ao vivo e do replay. É o
que permite a uma gravação de áudio entrar no cano de recepção da estação sem
que nada a jusante saiba que ela veio de um arquivo de som.

## O que este adapter é, e o que ele NÃO é

Um WAV do gqrx em Narrow FM **não é IQ**: é a saída do discriminador de
frequência, porque o Narrow FM já fez essa etapa. O que está gravado ali é a
frequência instantânea do sinal ao longo do tempo.

Este adapter faz o caminho inverso: integra essa frequência de volta à fase e
gera banda-base complexa.

    fase = soma acumulada do áudio × k
    iq   = exp(j · fase)

Passado pelo discriminador do `grs-demodulator`, esse IQ devolve o áudio
original — então o cano inteiro, do demodulador ao detector de syncword, roda
sobre sinal de antena de verdade.

É RECONSTRUÇÃO, e vale saber o que ela não alcança:

- **amplitude não é preservada.** O resultado tem envoltória constante, por
  construção. Nada que dependa de amplitude — AGC, rejeição de canal
  adjacente, detecção de desvanecimento — está sendo exercitado.
- **o que o gqrx fez está embutido.** Filtro de recepção, AGC e squelch já
  agiram sobre esse áudio, e não há como desfazê-los.
- **não há nada fora do canal.** O gqrx já filtrou; o espectro reconstruído
  tem só o que sobreviveu àquele filtro.

O que ela traz, e nenhum simulador inventa: ruído real, resíduo de Doppler,
desvanecimento, e o relógio de um oscilador que esteve em órbita.
"""

from __future__ import annotations

import pathlib
import wave
from datetime import datetime, timezone
from typing import Iterator

import numpy as np

from iq_recorder.domain.models import CaptureProfile, FrequencySource, IqBlock, SampleFormat

DEFAULT_BLOCK_SAMPLES = 8192

# Desvio de pico da portadora reconstruída, em fração da taxa de símbolo. 0.25
# corresponde a índice de modulação 0.5 — o caso GMSK, e o que o demodulador
# da estação assume.
#
# O valor absoluto importa menos do que parece: o fatiador decide por sinal
# (>0 ou <=0) e o discriminador do demodulador remove a média antes. O que
# precisa estar certo é a FORMA, e ela vem do áudio.
DEFAULT_DEVIATION_RATIO = 0.25


class WavIqSource:
    """Implementa IqStreamSource reconstruindo IQ de um WAV do gqrx."""

    def __init__(
        self,
        path: str | pathlib.Path,
        baud: int,
        block_samples: int = DEFAULT_BLOCK_SAMPLES,
        deviation_ratio: float = DEFAULT_DEVIATION_RATIO,
        max_seconds: float | None = None,
    ) -> None:
        self.path = pathlib.Path(path)

        if not self.path.exists():
            raise FileNotFoundError(f"WAV não encontrado: {self.path}")
        if baud <= 0:
            raise ValueError(f"baud precisa ser positivo, veio {baud}")
        if block_samples <= 0:
            raise ValueError("block_samples precisa ser positivo")

        self.baud = baud
        self.block_samples = block_samples

        audio, self.sample_rate_hz = self._read(max_seconds)

        self.samples_per_symbol = self.sample_rate_hz / baud
        if self.samples_per_symbol < 2:
            raise ValueError(
                f"{self.sample_rate_hz} Hz a {baud} baud dá {self.samples_per_symbol:.1f} "
                "amostras por símbolo; abaixo de 2 não há o que recuperar. Confira o baud."
            )

        self._iq = self._to_iq(audio, deviation_ratio)

    # --- IqStreamSource -----------------------------------------------------

    def blocks(self) -> Iterator[IqBlock]:
        raw = self._iq.tobytes()
        chunk = self.block_samples * SampleFormat.CF32_LE.bytes_per_sample

        for sequence, start in enumerate(range(0, len(raw), chunk)):
            yield IqBlock(
                data=raw[start : start + chunk],
                sequence=sequence,
                received_at=datetime.now(timezone.utc),
            )

    def close(self) -> None:
        pass

    # --- detalhes -----------------------------------------------------------

    @property
    def sample_count(self) -> int:
        return len(self._iq)

    @property
    def duration_seconds(self) -> float:
        return len(self._iq) / self.sample_rate_hz

    def profile(self, name: str, center_frequency_hz: float) -> CaptureProfile:
        """O CaptureProfile que descreve esta reconstrução.

        A frequência é a que o operador diz ter sintonizado no gqrx. Ela NÃO
        está no WAV — áudio não carrega a portadora — e por isso é declarada,
        nunca observada.
        """
        return CaptureProfile(
            name=name,
            description=(
                f"Reconstruído de {self.path.name} (áudio do gqrx, Narrow FM) "
                f"a {self.baud} baud"
            ),
            center_frequency_hz=center_frequency_hz,
            sample_rate_hz=float(self.sample_rate_hz),
            datatype=SampleFormat.CF32_LE,
            # O ganho do gqrx não está no arquivo, e inventar um número seria
            # afirmar algo sobre o hardware que ninguém mediu.
            gain_db=None,
            rf_path=f"antena -> SDR -> gqrx (Narrow FM) -> {self.path.name} -> IQ reconstruído",
            frequency_source=FrequencySource.FIXED,
        )

    def _read(self, max_seconds: float | None) -> tuple[np.ndarray, int]:
        with wave.open(str(self.path), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            raw = handle.readframes(handle.getnframes())

        if width != 2:
            raise ValueError(f"esperava WAV de 16 bits, veio {width * 8}")

        audio = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0

        if channels > 1:
            # Média dos canais. O gqrx grava mono, mas uma gravação de outra
            # fonte pode não ser — e usar só um canal daria metade da energia
            # sem dizer por quê.
            audio = audio.reshape(-1, channels).mean(axis=1)

        if max_seconds is not None:
            audio = audio[: int(max_seconds * rate)]

        if len(audio) == 0:
            raise ValueError(f"{self.path.name} não tem amostras")

        return audio, rate

    def _to_iq(self, audio: np.ndarray, deviation_ratio: float) -> np.ndarray:
        """Integra a frequência de volta à fase."""
        # Remove a média ANTES de integrar. O áudio de um discriminador carrega
        # em DC o desvio da portadora em relação ao centro sintonizado, e
        # integrar um DC produz uma rampa de fase — ou seja, uma portadora
        # deslocada que cresce sem parar. O demodulador removeria a média
        # depois, mas a essa altura a rampa já teria empurrado o sinal para
        # fora da banda.
        centered = audio - audio.mean()

        peak = np.max(np.abs(centered))
        if peak > 0:
            centered = centered / peak

        # Radianos por amostra no desvio de pico.
        step = 2.0 * np.pi * (deviation_ratio * self.baud) / self.sample_rate_hz

        return np.exp(1j * np.cumsum(centered) * step).astype(np.complex64)
