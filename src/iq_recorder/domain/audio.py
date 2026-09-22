"""Demodulação FM do fluxo de IQ — o caminho de áudio.

O mesmo *tap* na :5556 que alimenta a gravação alimenta isto. É a troca de
camadas que o desenho hexagonal prometia: `IqStreamSource` na entrada,
`AudioSink` na saída, e o resto do cano não fica sabendo.

## Por que isto é curto

O discriminador de frequência — `angle(x[n] * conj(x[n-1]))` — **é** um
demodulador FM. Ele já existe no `grs-demodulator`, que o usa como primeiro
estágio antes do filtro casado e da recuperação de tempo, e depois joga o
resultado fora (`soft_symbols, _ = demodulate(...)`). Aquele `_` descartado é
o áudio.

Os dois caminhos compartilham o discriminador e divergem logo depois:

    voz FM   discriminador -> de-ênfase -> decimação -> áudio
    2GFSK    discriminador -> filtro casado -> recuperação de tempo -> bits

## O que este módulo não faz

Sem scipy: o passa-baixas é um sinc janelado montado com numpy. O gravador
não tem scipy nas dependências e não vale ganhá-la por dez linhas de filtro.
"""

from __future__ import annotations

import numpy as np

DEFAULT_AUDIO_RATE_HZ = 48_000

# 75 µs é a constante de tempo de de-ênfase das Américas (a Europa usa 50 µs).
# O transmissor levanta os agudos para melhorar a relação sinal-ruído; o
# receptor tem de baixá-los de volta, ou o áudio sai metálico e chiado.
DEFAULT_DEEMPHASIS_US = 75.0

# Banda do áudio recuperado. 15 kHz é o teto da FM comercial.
DEFAULT_AUDIO_BANDWIDTH_HZ = 15_000

# Desvio de frequência de pico. 75 kHz é o padrão de broadcast; valores
# menores são banda estreita. Serve para normalizar a amplitude: o áudio sai
# em ±1 quando o sinal usa o desvio inteiro.
DEFAULT_DEVIATION_HZ = 75_000.0


def lowpass_taps(cutoff_hz: float, sample_rate_hz: float, num_taps: int = 127) -> np.ndarray:
    """Passa-baixas FIR por sinc janelado (Hamming).

    Ímpar de propósito: um número par de taps atrasa o sinal por meia amostra,
    e meia amostra de atraso num caminho que depois decima é fase que não se
    recupera.
    """
    if num_taps % 2 == 0:
        num_taps += 1
    if not 0 < cutoff_hz < sample_rate_hz / 2:
        raise ValueError(
            f"corte {cutoff_hz} Hz fora de Nyquist para {sample_rate_hz} Hz"
        )

    n = np.arange(num_taps) - (num_taps - 1) / 2
    fc = cutoff_hz / sample_rate_hz
    taps = 2 * fc * np.sinc(2 * fc * n) * np.hamming(num_taps)

    return (taps / taps.sum()).astype(np.float64)


class FmAudioDemodulator:
    """IQ em banda-base -> áudio, mantendo estado entre blocos.

    Feito para fluxo: `process()` é chamado bloco a bloco e cada chamada
    continua de onde a anterior parou. Três estados atravessam a fronteira, e
    esquecer qualquer um deles produz um defeito audível:

    - a ÚLTIMA AMOSTRA do bloco anterior. O discriminador olha a diferença de
      fase entre amostras vizinhas, então sem ela a primeira amostra de cada
      bloco não tem par e vira um estalo. É exatamente o defeito que a bancada
      do cano de bits expôs: lá, o `np.concatenate([[0], ...])` insere um zero
      falso em toda borda de janela, e os pacotes que atravessam uma borda saem
      corrompidos;
    - o estado do filtro passa-baixas, sem o qual cada bloco recomeça de zero
      e produz um transitório;
    - a fase da decimação, sem a qual a taxa de áudio flutua em torno da
      nominal.
    """

    def __init__(
        self,
        sample_rate_hz: int,
        audio_rate_hz: int = DEFAULT_AUDIO_RATE_HZ,
        deviation_hz: float = DEFAULT_DEVIATION_HZ,
        deemphasis_us: float | None = DEFAULT_DEEMPHASIS_US,
        audio_bandwidth_hz: float = DEFAULT_AUDIO_BANDWIDTH_HZ,
    ) -> None:
        if sample_rate_hz <= 0 or audio_rate_hz <= 0:
            raise ValueError("as taxas precisam ser positivas")
        if sample_rate_hz % audio_rate_hz != 0:
            # Decimação fracionária exigiria reamostrador, e um reamostrador
            # mal feito é uma fonte silenciosa de desafinação. 240000/48000 = 5
            # exato, que é o que a estação usa.
            raise ValueError(
                f"{sample_rate_hz} não é múltiplo inteiro de {audio_rate_hz}; "
                "escolha uma taxa de IQ que decime redondo"
            )
        if deviation_hz <= 0:
            raise ValueError("o desvio precisa ser positivo")

        self.sample_rate_hz = sample_rate_hz
        self.audio_rate_hz = audio_rate_hz
        self.decimation = sample_rate_hz // audio_rate_hz
        self.deviation_hz = deviation_hz

        cutoff = min(audio_bandwidth_hz, audio_rate_hz / 2 * 0.9)
        self._taps = lowpass_taps(cutoff, sample_rate_hz)
        self._filter_state = np.zeros(len(self._taps) - 1, dtype=np.float64)

        self._last_sample: complex | None = None
        self._decimation_phase = 0

        self._deemph_alpha = 0.0
        self._deemph_state = 0.0
        if deemphasis_us:
            tau = deemphasis_us * 1e-6
            self._deemph_alpha = float(np.exp(-1.0 / (audio_rate_hz * tau)))

    @property
    def gain(self) -> float:
        """Fator que leva radianos por amostra a ±1 no desvio de pico."""
        return self.sample_rate_hz / (2.0 * np.pi * self.deviation_hz)

    def _discriminate(self, samples: np.ndarray) -> np.ndarray:
        """Frequência instantânea, em ±1 relativo ao desvio de pico."""
        if self._last_sample is not None:
            samples = np.concatenate(([self._last_sample], samples))

        if len(samples) < 2:
            self._last_sample = samples[-1] if len(samples) else self._last_sample
            return np.zeros(0, dtype=np.float64)

        self._last_sample = complex(samples[-1])

        # O produto pelo conjugado da amostra anterior dá a diferença de fase
        # já desembrulhada em (-π, π] — sem np.unwrap, que quebraria em fluxo.
        delta = np.angle(samples[1:] * np.conj(samples[:-1]))

        return delta.astype(np.float64) * self.gain

    def _filter(self, audio: np.ndarray) -> np.ndarray:
        """Passa-baixas com estado, por convolução com sobreposição-salvamento."""
        if len(audio) == 0:
            return audio

        padded = np.concatenate((self._filter_state, audio))
        self._filter_state = padded[-(len(self._taps) - 1) :]

        return np.convolve(padded, self._taps, mode="valid")

    def _decimate(self, audio: np.ndarray) -> np.ndarray:
        """Pega uma amostra a cada `decimation`, preservando a fase entre blocos."""
        if len(audio) == 0:
            return audio

        taken = audio[self._decimation_phase :: self.decimation]
        consumed = len(audio) - self._decimation_phase
        self._decimation_phase = (-consumed) % self.decimation

        return taken

    def _deemphasize(self, audio: np.ndarray) -> np.ndarray:
        """Um polo IIR: y[n] = (1-a)·x[n] + a·y[n-1].

        Laço explícito porque é recursivo — numpy não vetoriza dependência do
        próprio passado anterior. Roda sobre o áudio já decimado, que é 1/N do
        volume de amostras.
        """
        if self._deemph_alpha == 0.0 or len(audio) == 0:
            return audio

        out = np.empty_like(audio)
        state = self._deemph_state
        one_minus = 1.0 - self._deemph_alpha

        for index, value in enumerate(audio):
            state = one_minus * value + self._deemph_alpha * state
            out[index] = state

        self._deemph_state = float(state)

        # A de-ênfase tira energia; recompõe o ganho para o áudio não sair
        # baixo demais a ponto de o operador subir o volume e ouvir o ruído.
        return out / one_minus if one_minus > 0 else out

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Um bloco de IQ (complex64) -> áudio float64 em ±1.

        O retorno pode ser mais curto ou mais longo que len(samples)/decimation
        por uma amostra: a fase da decimação atravessa os blocos.
        """
        if samples.dtype != np.complex64 and samples.dtype != np.complex128:
            samples = samples.astype(np.complex64)

        return self._deemphasize(self._decimate(self._filter(self._discriminate(samples))))


def to_pcm16(audio: np.ndarray, headroom_db: float = 1.0) -> bytes:
    """Áudio em ±1 -> PCM 16 bits little-endian, normalizado pelo pico.

    Normaliza pelo pico em vez de assumir que o sinal usa o desvio inteiro: uma
    gravação de banda estreita sairia quase inaudível se fosse escalada pelo
    desvio nominal de broadcast, e o operador concluiria que o cano não
    funciona quando o problema era o volume.
    """
    if len(audio) == 0:
        return b""

    peak = float(np.max(np.abs(audio)))
    if peak <= 0:
        return np.zeros(len(audio), dtype="<i2").tobytes()

    scale = (10.0 ** (-headroom_db / 20.0)) / peak

    return np.clip(audio * scale * 32767.0, -32768, 32767).astype("<i2").tobytes()
