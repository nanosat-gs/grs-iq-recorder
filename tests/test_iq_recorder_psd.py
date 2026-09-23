"""Testes da leitura mínima (D1).

O que se confere é se o espectro DIZ A VERDADE sobre o sinal: um tom plantado
num offset conhecido tem de aparecer naquele offset, e ruído puro tem de ser
reconhecido como ruído. Um PSD que erra isso é pior que nenhum — manda alguém
procurar defeito no lugar errado.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from iq_recorder.adapters.numpy_psd_view import NumpyPsdView
from iq_recorder.domain.models import CaptureProfile, IqBlock, SampleFormat

START = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE_RATE = 240_000

PROFILE = CaptureProfile(
    name="teste",
    description="perfil de teste",
    center_frequency_hz=145_900_000.0,
    sample_rate_hz=float(SAMPLE_RATE),
)


class ArraySource:
    """IqStreamSource sobre um array em memória."""

    def __init__(self, samples: np.ndarray, block: int = 8192) -> None:
        self._raw = samples.astype(np.complex64).tobytes()
        self._block = block * SampleFormat.CF32_LE.bytes_per_sample

    def blocks(self):
        for index, start in enumerate(range(0, len(self._raw), self._block)):
            yield IqBlock(self._raw[start : start + self._block], index, START)

    def close(self) -> None:
        pass


def tone_at(offset_hz: float, n: int = 65536) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE

    return np.exp(2j * np.pi * offset_hz * t)


def noise(n: int = 65536, seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)

    return rng.normal(size=n) + 1j * rng.normal(size=n)


def test_tom_aparece_no_offset_onde_foi_plantado():
    """O teste central do D1."""
    view = NumpyPsdView()

    summary = view.summarize(ArraySource(tone_at(30_000.0)), PROFILE)

    assert summary.peak_offset_hz == pytest.approx(30_000.0, abs=100.0)


def test_tom_no_centro_da_offset_zero():
    summary = NumpyPsdView().summarize(ArraySource(tone_at(0.0)), PROFILE)

    assert summary.peak_offset_hz == pytest.approx(0.0, abs=100.0)


def test_offset_negativo_e_reportado_como_negativo():
    """O sinal do offset diz de que lado do centro o sinal está — e inverter
    isso mandaria a correção de sintonia para o lado errado."""
    summary = NumpyPsdView().summarize(ArraySource(tone_at(-45_000.0)), PROFILE)

    assert summary.peak_offset_hz == pytest.approx(-45_000.0, abs=100.0)


def test_sinal_limpo_se_levanta_muito_do_piso():
    summary = NumpyPsdView().summarize(ArraySource(tone_at(10_000.0)), PROFILE)

    assert summary.has_signal
    assert summary.peak_above_floor_db > 30.0


def test_ruido_puro_nao_e_confundido_com_sinal():
    """Num espectro só de ruído o maior bin fica a poucos dB da mediana: não
    há nada de fato levantado. É este número que separa 'gravei a passagem' de
    'gravei o nada'."""
    summary = NumpyPsdView().summarize(ArraySource(noise()), PROFILE)

    assert not summary.has_signal
    assert summary.peak_above_floor_db < 15.0


def test_resumo_traz_duracao_e_contagem():
    summary = NumpyPsdView().summarize(ArraySource(tone_at(0.0, n=48_000)), PROFILE)

    assert summary.sample_count == 48_000
    assert summary.duration_seconds == pytest.approx(0.2)


def test_frequencias_sao_relativas_ao_centro():
    """Num espectro de banda-base o eixo natural é o desvio, não a frequência
    absoluta."""
    freqs, _ = NumpyPsdView().power_spectral_density(ArraySource(tone_at(0.0)), PROFILE)

    assert min(freqs) == pytest.approx(-SAMPLE_RATE / 2, abs=100.0)
    assert max(freqs) < SAMPLE_RATE / 2


def test_psd_e_normalizado_pelo_pico():
    """Potência absoluta dependeria do ganho do receptor, que a captura nem
    sempre conhece — o perfil registra 'automático' quando o AGC manda."""
    _, psd_db = NumpyPsdView().power_spectral_density(ArraySource(tone_at(5_000.0)), PROFILE)

    assert max(psd_db) == pytest.approx(0.0, abs=1e-9)


def test_media_de_segmentos_acha_o_pico_certo_em_meio_ao_ruido():
    """Num único FFT o piso é tão irregular que um pico ALEATÓRIO de ruído
    vence o sinal de verdade. A média é o que faz o ruído baixar e o tom ficar.

    Repare no que este teste NÃO afirma: que o pico fica mais alto acima do
    piso. Ele fica mais BAIXO — porque com o piso liso o tom fraco se destaca
    menos do que um espinho de ruído se destacava de um piso irregular. O que
    a média melhora é ACERTAR O LUGAR, e é isso que importa: um pico alto no
    offset errado manda alguém corrigir uma sintonia que estava certa."""
    tom_hz = 20_000.0
    sinal = tone_at(tom_hz, n=131_072) * 0.05 + noise(131_072)

    um_segmento = NumpyPsdView().summarize(ArraySource(sinal[:4096]), PROFILE)
    muitos = NumpyPsdView().summarize(ArraySource(sinal), PROFILE)

    assert muitos.peak_offset_hz == pytest.approx(tom_hz, abs=100.0)
    assert abs(um_segmento.peak_offset_hz - tom_hz) > 1000.0


def test_fft_size_que_nao_e_potencia_de_dois_e_recusado():
    with pytest.raises(ValueError, match="potência de 2"):
        NumpyPsdView().power_spectral_density(ArraySource(tone_at(0.0)), PROFILE, fft_size=1000)


def test_captura_curta_demais_e_recusada():
    with pytest.raises(ValueError, match="curta demais"):
        NumpyPsdView().summarize(ArraySource(tone_at(0.0, n=100)), PROFILE)


def test_teto_de_amostras_limita_a_leitura():
    """Uma passagem de dez minutos a 240 kS/s são 11 GB, e a resposta sobre
    'tem sinal aqui?' não melhora depois dos primeiros segundos."""
    summary = NumpyPsdView(max_samples=16_384).summarize(
        ArraySource(tone_at(0.0, n=200_000)), PROFILE
    )

    assert summary.sample_count <= 24_576  # teto + o resto do último lote


def test_max_samples_invalido_e_recusado():
    with pytest.raises(ValueError, match="max_samples"):
        NumpyPsdView(max_samples=0)
