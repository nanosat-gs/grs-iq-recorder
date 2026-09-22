"""Portas do GRS IQ Recorder.

São Protocols, e não classes-base: o estilo da estação é o do GRS Manager
(`domain/ports.py` lá é um Protocol de uma linha por método). Um adapter não
herda de nada — ele só precisa ter os métodos.

O desenho inteiro do serviço cabe numa observação: `ZmqIqSource` (o tap ao
vivo) e `FileIqSource` (o replay) implementam a MESMA porta. É isso que faz o
teste ponta a ponta rodar sem hardware — o resto do cano não consegue
distinguir um do outro.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol

from iq_recorder.domain.models import CaptureMetadata, CaptureProfile, IqBlock


class IqStreamSource(Protocol):
    """De onde vêm os lotes de IQ: do SDR ao vivo (ZmqIqSource) ou de uma
    captura em disco (FileIqSource).

    Iterator e não lista: uma passagem de LEO a 240 kS/s são ~11 GB, e nada
    aqui pode assumir que a captura cabe na memória.
    """

    def blocks(self) -> Iterator[IqBlock]: ...
    def close(self) -> None: ...


class IqSink(Protocol):
    """Para onde os lotes vão. A implementação desta fatia é o FileIqSink, que
    escreve o par SigMF (`.sigmf-data` + `.sigmf-meta`).

    `close()` devolve o CaptureMetadata porque só no fim se sabe o que a linha
    do índice precisa: quantas amostras entraram, quando terminou, e o hash do
    arquivo fechado.
    """

    def open(self, profile: CaptureProfile) -> None: ...
    def write(self, block: IqBlock) -> None: ...
    def close(self) -> CaptureMetadata: ...


class IqStreamPublisher(Protocol):
    """Republica lotes no MESMO tópico em que o `grs-iq-rx` publicaria.

    É o que torna o replay indistinguível do vivo: com o `grs-iq-rx` desligado,
    o demodulador e o detector de syncword continuam assinando :5556 e não têm
    como saber que do outro lado há um arquivo, e não um rádio.
    """

    def publish(self, block: IqBlock) -> None: ...
    def close(self) -> None: ...


class CaptureIndex(Protocol):
    """Índice append-only das capturas — nunca UPDATE, nunca DELETE.

    Append-only porque uma captura é uma observação: ela aconteceu, num
    instante, com uma configuração. Reescrever a linha depois é reescrever o
    que a estação viu, e é assim que uma regressão de DSP passa a ser
    impossível de reproduzir.
    """

    def append(self, metadata: CaptureMetadata) -> None: ...
    def list_captures(self, limit: int = 100) -> Iterable[CaptureMetadata]: ...
    def get(self, capture_id: str) -> CaptureMetadata | None: ...


class SpectrumView(Protocol):
    """A leitura mínima: um PSD e um resumo de uma captura.

    Existe para responder "tem sinal aqui?" antes de alguém gastar uma tarde
    caçando bug no demodulador por causa de uma captura de ruído.
    """

    def power_spectral_density(
        self, source: IqStreamSource, profile: CaptureProfile, fft_size: int = 4096
    ) -> tuple[list[float], list[float]]: ...
