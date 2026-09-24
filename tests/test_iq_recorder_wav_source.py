"""Testes do WavIqSource — áudio do gqrx virando IQ.

O que se confere é a propriedade que torna a reconstrução útil: passar o IQ
gerado por um discriminador de frequência tem de devolver o áudio original.
Se isso não valer, o cano estaria sendo exercitado contra um sinal que ninguém
recebeu.
"""

from __future__ import annotations

import pathlib
import wave

import numpy as np
import pytest

from iq_recorder.adapters.wav_iq_source import WavIqSource
from iq_recorder.domain.models import FrequencySource, SampleFormat

SAMPLE_RATE = 48_000
BAUD = 1200


def write_wav(path: pathlib.Path, audio: np.ndarray, rate: int = SAMPLE_RATE,
              channels: int = 1) -> pathlib.Path:
    data = np.clip(audio * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    return path


def square_symbols(bits: list[int], sps: int) -> np.ndarray:
    """Áudio como um discriminador o entregaria: nível por símbolo."""
    return np.repeat([1.0 if b else -1.0 for b in bits], sps)


def discriminate(iq: np.ndarray) -> np.ndarray:
    """O mesmo cálculo do grs-demodulator: diferença de fase entre vizinhos."""
    return np.angle(iq[1:] * np.conj(iq[:-1]))


def read_iq(source: WavIqSource) -> np.ndarray:
    raw = b"".join(block.data for block in source.blocks())
    return np.frombuffer(raw, dtype=np.complex64)


# --- a propriedade central ---------------------------------------------------


def test_o_discriminador_devolve_o_audio_original(tmp_path):
    """A ida e a volta se cancelam.

    É isto que autoriza usar a reconstrução no lugar de IQ de antena: o
    demodulador da estação começa exatamente por este cálculo.
    """
    bits = [1, 0, 0, 1, 1, 1, 0, 1, 0, 0] * 20
    audio = square_symbols(bits, sps=SAMPLE_RATE // BAUD)
    wav = write_wav(tmp_path / "t.wav", audio)

    recuperado = discriminate(read_iq(WavIqSource(wav, baud=BAUD)))

    # Correlação, e não igualdade: a amplitude é reescalada por construção, e
    # o que precisa sobreviver é a FORMA.
    alvo = audio[1:]
    correlacao = np.corrcoef(recuperado, alvo)[0, 1]

    assert correlacao > 0.99


def test_o_sinal_dos_simbolos_e_preservado(tmp_path):
    """Inverter o sinal inverteria todos os bits, e o syncword não apareceria
    — mas nada estouraria, o que é o pior tipo de defeito."""
    bits = [1] * 40 + [0] * 40
    audio = square_symbols(bits, sps=SAMPLE_RATE // BAUD)
    wav = write_wav(tmp_path / "t.wav", audio)

    recuperado = discriminate(read_iq(WavIqSource(wav, baud=BAUD)))
    meio = len(recuperado) // 2

    assert recuperado[:meio].mean() > 0
    assert recuperado[meio:].mean() < 0


# --- o que a reconstrução não preserva ---------------------------------------


def test_a_envoltoria_sai_constante(tmp_path):
    """Por construção. Nada que dependa de amplitude está sendo exercitado, e
    o docstring do adapter diz isso em voz alta."""
    audio = square_symbols([1, 0] * 100, sps=SAMPLE_RATE // BAUD) * 0.1
    wav = write_wav(tmp_path / "t.wav", audio)

    assert np.allclose(np.abs(read_iq(WavIqSource(wav, baud=BAUD))), 1.0, atol=1e-5)


def test_o_dc_do_audio_nao_desloca_o_sinal(tmp_path):
    """Um discriminador carrega em DC o desvio da portadora em relação ao
    centro sintonizado. Integrar esse DC produziria uma rampa de fase — uma
    portadora que foge da banda — e o sinal sairia do centro sem que nada
    avisasse.

    O teste é DIFERENCIAL de propósito. A primeira versão dele exigia que o
    pico do espectro caísse perto de zero, e falhava: com símbolos alternados
    o pico cai numa banda lateral em ±600 Hz, tenha havido DC ou não. O que
    importa não é ONDE está o pico — é que somar DC ao áudio não o mova.
    """
    simbolos = square_symbols([1, 0] * 200, sps=SAMPLE_RATE // BAUD)
    sem_dc = write_wav(tmp_path / "a.wav", simbolos)
    com_dc = write_wav(tmp_path / "b.wav", simbolos + 0.5)

    def pico(caminho):
        iq = read_iq(WavIqSource(caminho, baud=BAUD))[: 2**14]
        espectro = np.abs(np.fft.fftshift(np.fft.fft(iq)))
        freqs = np.fft.fftshift(np.fft.fftfreq(2**14, 1.0 / SAMPLE_RATE))
        return float(freqs[int(np.argmax(espectro))])

    assert pico(com_dc) == pytest.approx(pico(sem_dc), abs=10.0)


# --- contrato e validação ----------------------------------------------------


def test_o_perfil_declara_a_frequencia_como_nao_observada(tmp_path):
    """Áudio não carrega portadora: a frequência é o que o operador diz ter
    sintonizado, e o sidecar tem de registrar isso como declarado."""
    wav = write_wav(tmp_path / "t.wav", square_symbols([1, 0] * 50, 40))

    profile = WavIqSource(wav, baud=BAUD).profile("teste", 145_900_000.0)

    assert profile.frequency_source is FrequencySource.FIXED
    assert profile.center_frequency_hz == 145_900_000.0
    assert profile.gain_db is None
    assert profile.datatype is SampleFormat.CF32_LE


def test_a_taxa_do_perfil_vem_do_arquivo(tmp_path):
    wav = write_wav(tmp_path / "t.wav", square_symbols([1, 0] * 50, 20), rate=24_000)

    assert WavIqSource(wav, baud=BAUD).profile("t", 1.0).sample_rate_hz == 24_000.0


def test_estereo_vira_mono_pela_media(tmp_path):
    """Usar só um canal daria metade da energia sem dizer por quê."""
    mono = square_symbols([1, 0] * 50, 40)
    estereo = np.repeat(mono, 2)
    wav = write_wav(tmp_path / "t.wav", estereo, channels=2)

    assert WavIqSource(wav, baud=BAUD).sample_count == len(mono)


def test_baud_alto_demais_para_a_taxa_e_recusado(tmp_path):
    """Menos de 2 amostras por símbolo não tem o que recuperar — e o erro mais
    provável é o operador ter posto o baud do downlink num beacon."""
    wav = write_wav(tmp_path / "t.wav", square_symbols([1, 0] * 50, 40))

    with pytest.raises(ValueError, match="amostras por símbolo"):
        WavIqSource(wav, baud=48_000)


def test_arquivo_ausente_e_erro_claro(tmp_path):
    with pytest.raises(FileNotFoundError):
        WavIqSource(tmp_path / "nao-existe.wav", baud=BAUD)


def test_recorte_por_tempo(tmp_path):
    wav = write_wav(tmp_path / "t.wav", square_symbols([1, 0] * 600, 40))

    source = WavIqSource(wav, baud=BAUD, max_seconds=0.5)

    assert source.sample_count == pytest.approx(SAMPLE_RATE * 0.5, abs=2)


def test_os_lotes_somam_a_captura_inteira(tmp_path):
    """A mesma porta do tap ao vivo: quem consome não sabe de onde veio."""
    wav = write_wav(tmp_path / "t.wav", square_symbols([1, 0] * 300, 40))
    source = WavIqSource(wav, baud=BAUD, block_samples=1000)

    total = sum(len(block.data) for block in source.blocks())

    assert total == source.sample_count * SampleFormat.CF32_LE.bytes_per_sample
