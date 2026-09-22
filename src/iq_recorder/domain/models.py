"""Value objects do GRS IQ Recorder.

Tudo aqui é imutável e não sabe nada de ZMQ, de arquivo nem de banco: é o
vocabulário que as portas (ports.py) trocam entre si. O gravador é o único
bloco da metade de RF que a equipe escreve do zero, e é deliberado que ele
não importe nada dos blocos adotados — a fronteira entre eles é rede (ZMQ),
como entre todos os serviços da estação.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SampleFormat(str, Enum):
    """Formato de amostra, no vocabulário do SigMF.

    Um valor só, e isso é uma afirmação sobre o caminho de dados, não falta de
    imaginação: o `grs-iq-rx` publica `struct { float i; float q; }` sem
    cabeçalho nenhum, o `grs-demodulator` lê aquilo com
    `np.frombuffer(buf, dtype=np.complex64)`, e as duas pontas estão de acordo
    hoje. Enquanto for assim, oferecer outros formatos seria oferecer uma
    escolha que ninguém pode fazer.
    """

    CF32_LE = "cf32_le"

    @property
    def bytes_per_sample(self) -> int:
        return 8  # float32 I + float32 Q


class FrequencySource(str, Enum):
    """Como o gravador descobre a frequência efetiva da captura.

    A captura precisa ser auto-descritiva — daqui a seis meses, o arquivo tem
    de dizer sozinho em que frequência foi gravado, sem depender de alguém
    lembrar qual era o .env daquele dia.

    FIXED       a frequência é a do perfil, e ponto. É o caso hoje: o
                `grs-iq-rx` não tem retune, então ele sintoniza no boot e fica.
    TUNE_TOPIC  a frequência vem do `frequency-synthesizer`, que publica
                `[b"tune", <freq Hz em ASCII>]` num PUB :5557. É o caminho do
                Doppler, e está FORA desta fatia: o contrato já está mapeado,
                mas ninguém assina esse tópico ainda.
    """

    FIXED = "fixed"
    TUNE_TOPIC = "tune_topic"


@dataclass(frozen=True)
class CaptureProfile:
    """O equivalente RX do MissionProfile: o que uma captura carrega para ser
    auto-descritiva.

    Descreve a configuração de recepção — o que sintonizar, a que taxa, com
    que ganho, por qual caminho de RF — e é a partir dele que o sidecar SigMF
    é escrito (ver domain/capture.py).
    """

    name: str
    description: str
    center_frequency_hz: float
    sample_rate_hz: float
    datatype: SampleFormat = SampleFormat.CF32_LE
    # None = ganho automático (AGC do tuner). O `grs-iq-rx` trata `-g 0` como
    # automático, e é assim que ele sai da caixa.
    gain_db: float | None = None
    rf_path: str = ""
    frequency_source: FrequencySource = FrequencySource.FIXED

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CaptureProfile exige um nome")
        if self.center_frequency_hz <= 0:
            raise ValueError(f"frequência central inválida: {self.center_frequency_hz}")
        if self.sample_rate_hz <= 0:
            raise ValueError(f"taxa de amostragem inválida: {self.sample_rate_hz}")

    @property
    def bytes_per_second(self) -> float:
        """Vazão do fluxo de IQ. Serve para dimensionar disco antes de gravar.

        Não é um detalhe: a 240 kS/s são 1,9 MB/s, ou ~11 GB numa passagem de
        LEO de dez minutos. Gravar "a passagem inteira" sem olhar para este
        número é como uma campanha de gravação enche um disco em silêncio.
        """
        return self.sample_rate_hz * self.datatype.bytes_per_sample


@dataclass(frozen=True)
class IqBlock:
    """Um lote de amostras de IQ, como chega do PUB :5556.

    LOTE, e não amostra: hoje a impl. C do `grs-iq-rx` faz um `zmq_send` por
    amostra (8 bytes por mensagem), o que não segura taxa real e obrigaria
    este gravador a um `recv()` por amostra. Lotear aquele envelope é o
    primeiro conserto do B1, e é este tipo que descreve o resultado.

    `data` são bytes crus, no formato do perfil — o gravador não desempacota
    para float. Desempacotar seria trabalho jogado fora: o que vai para o
    arquivo é exatamente o que veio do socket, byte a byte, e é isso que faz
    o replay ser indistinguível do vivo.
    """

    data: bytes
    sequence: int
    received_at: datetime

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError(f"sequence não pode ser negativo: {self.sequence}")

    def sample_count(self, datatype: SampleFormat = SampleFormat.CF32_LE) -> int:
        """Quantas amostras completas o lote carrega.

        Divisão inteira de propósito: um lote truncado (o socket entregou meia
        amostra) não é erro deste tipo, é sintoma de envelope mal definido lá
        na origem. Quem decide o que fazer com a sobra é o sink.
        """
        return len(self.data) // datatype.bytes_per_sample


@dataclass(frozen=True)
class CaptureMetadata:
    """A linha do índice, e o resumo que acompanha uma captura gravada.

    `sha512` é do arquivo de amostras (o `.sigmf-data`), não do sidecar: o
    sidecar ganha anotação depois da gravação (o PSD do D1, a contagem de
    frames do D2), enquanto as amostras são imutáveis por definição. Hash de
    coisa que muda não serve para verificar nada.

    SHA-512 e não SHA-256 porque o campo é `core:sha512` no SigMF. Guardar
    sha256 no índice daria duas somas para a mesma captura, e a do índice não
    bateria com a do sidecar — que é justamente a conferência que interessa.
    """

    capture_id: str
    profile_name: str
    center_frequency_hz: float
    sample_rate_hz: float
    datatype: SampleFormat
    started_at: datetime
    ended_at: datetime
    sample_count: int
    sha512: str
    data_path: str

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()
