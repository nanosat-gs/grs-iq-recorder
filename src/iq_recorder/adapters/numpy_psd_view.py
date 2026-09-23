"""Adapter de saída: espectro e resumo de uma captura (D1).

Implementa `SpectrumView`. A leitura mínima da fatia — o que responde, em
segundos, à pergunta que antecede qualquer depuração de DSP: *o que foi
gravado é sinal ou ruído?*

Sem isso, uma captura de ruído vira uma tarde caçando um bug no demodulador
que não existe.

Consome um `IqStreamSource`, então serve tanto a um arquivo quanto ao tap ao
vivo — de novo a mesma porta, de novo sem o chamador saber qual dos dois está
do outro lado.
"""

from __future__ import annotations

import numpy as np

from iq_recorder.domain.models import CaptureProfile, CaptureSummary, SampleFormat

DEFAULT_FFT_SIZE = 4096

# Teto de amostras a ler. Uma passagem de dez minutos a 240 kS/s são 11 GB, e
# nenhuma resposta sobre "tem sinal aqui?" melhora depois dos primeiros
# segundos. Ler tudo só atrasaria a resposta e encheria a memória.
DEFAULT_MAX_SAMPLES = 2_000_000


class NumpyPsdView:
    """Implementa SpectrumView com numpy."""

    def __init__(self, max_samples: int = DEFAULT_MAX_SAMPLES) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples precisa ser positivo")

        self.max_samples = max_samples

    # --- SpectrumView -------------------------------------------------------

    def power_spectral_density(
        self,
        source,
        profile: CaptureProfile,
        fft_size: int = DEFAULT_FFT_SIZE,
    ) -> tuple[list[float], list[float]]:
        """:return: (frequências em Hz relativas ao centro, potência em dB).

        As frequências são RELATIVAS à sintonia, e não absolutas. Num espectro
        de banda-base o eixo natural é o desvio: "o sinal está 30 kHz acima do
        centro" é a frase útil; "o sinal está em 145,93 MHz" obriga quem lê a
        fazer a subtração de cabeça.

        Média de segmentos com janela de Hann — Welch simplificado. A média é o
        que faz o ruído baixar e o sinal ficar: num único FFT o piso é tão
        irregular que qualquer pico parece significativo.
        """
        samples = self._read(source, profile)
        freqs, psd_db = self._psd_from(samples, profile, fft_size)

        return freqs.tolist(), psd_db.tolist()

    # --- resumo -------------------------------------------------------------

    def summarize(
        self,
        source,
        profile: CaptureProfile,
        fft_size: int = DEFAULT_FFT_SIZE,
    ) -> CaptureSummary:
        """Resumo legível de uma captura: duração, sintonia, pico, piso."""
        samples = self._read(source, profile)

        freqs, psd_db = self._psd_from(samples, profile, fft_size)

        peak_index = int(np.argmax(psd_db))
        # A MEDIANA como piso, não a média: a média é puxada para cima pelo
        # próprio pico, e num sinal forte o piso pareceria mais alto do que é.
        floor_db = float(np.median(psd_db))

        return CaptureSummary(
            sample_count=len(samples),
            duration_seconds=len(samples) / profile.sample_rate_hz,
            center_frequency_hz=profile.center_frequency_hz,
            sample_rate_hz=profile.sample_rate_hz,
            peak_offset_hz=float(freqs[peak_index]),
            peak_above_floor_db=float(psd_db[peak_index] - floor_db),
        )

    # --- detalhes -----------------------------------------------------------

    def _read(self, source, profile: CaptureProfile) -> np.ndarray:
        """Lê até `max_samples` da origem, como complex64."""
        if profile.datatype is not SampleFormat.CF32_LE:
            raise ValueError(f"formato não suportado: {profile.datatype}")

        payloads: list[bytes] = []
        total = 0
        limit = self.max_samples * profile.datatype.bytes_per_sample

        for block in source.blocks():
            payloads.append(block.data)
            total += len(block.data)
            if total >= limit:
                break

        raw = b"".join(payloads)

        # Só amostras completas: um resto significa que a origem entregou meia
        # amostra, e interpretá-la deslocaria todas as seguintes em quatro
        # bytes — o espectro sairia irreconhecível, sem nenhum erro.
        usable = (len(raw) // profile.datatype.bytes_per_sample) * profile.datatype.bytes_per_sample

        return np.frombuffer(raw[:usable], dtype=np.complex64)

    def _psd_from(
        self, samples: np.ndarray, profile: CaptureProfile, fft_size: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """O cálculo, num lugar só: o PSD público e o resumo consomem daqui.

        Duas cópias desta conta divergiriam — e divergiriam em silêncio, porque
        as duas continuariam devolvendo um espectro plausível.
        """
        if fft_size < 8 or (fft_size & (fft_size - 1)) != 0:
            raise ValueError(f"fft_size precisa ser potência de 2 e >= 8, veio {fft_size}")

        if len(samples) < fft_size:
            raise ValueError(
                f"captura curta demais: {len(samples)} amostras para um FFT de {fft_size}"
            )

        window = np.hanning(fft_size)
        segments = len(samples) // fft_size
        accumulator = np.zeros(fft_size)

        for index in range(segments):
            chunk = samples[index * fft_size : (index + 1) * fft_size] * window
            accumulator += np.abs(np.fft.fftshift(np.fft.fft(chunk))) ** 2

        psd_db = 10.0 * np.log10(accumulator / segments + 1e-20)
        psd_db -= psd_db.max()

        freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / profile.sample_rate_hz))

        return freqs, psd_db
