"""Adapter de entrada: IQ real de um USRP N210, via UHD.

Implementa `IqStreamSource`. Diferente do `RtlTcpIqSource`, aqui NÃO dá para
escrever um cliente de protocolo do zero: a Ettus não documenta o protocolo
de rede do USRP para uso por terceiros — só recomenda usar a biblioteca deles
(UHD, "USRP Hardware Driver"). Este adapter é uma camada fina sobre o UHD, não
uma reimplementação de protocolo.

## O que foi CONFIRMADO antes de escrever este arquivo, e como

Ao contrário do resto do cano, esta peça não pôde ser testada contra o
hardware real — não há um USRP N210 disponível. O que foi possível, e foi
feito, foi confirmar que o CAMINHO DE SOFTWARE está certo:

1. O pacote Debian `python3-uhd` (bookworm, versão 4.3.0.0+ds1-5) foi
   instalado e importado de verdade, dentro de um container.
2. A API de cada classe usada aqui foi INSPECIONADA no pacote instalado —
   `help()`, docstrings, construção real dos objetos — não copiada de um
   exemplo da documentação (que é de uma versão mais nova do UHD e já
   diverge: o exemplo oficial usa `uhd.usrp.rx_streamer`, mas o pacote do
   Debian expõe `uhd.usrp.RXStreamer`).
3. `uhd.usrp.MultiUSRP("addr=<ip inexistente>")` foi testado de verdade:
   falha em 0,7 s com RuntimeError, não trava. Por isso este adapter não
   precisa de um timeout de conexão próprio — o UHD já tem o dele.

O que NÃO foi confirmado, porque exige o rádio físico: se o `recv()`
realmente entrega amostras, se `set_rx_freq` de fato sintoniza, e qualquer
coisa que dependa do firmware/FPGA específico deste N210.

## Formato de saída

`get_rx_stream` foi pedido em `cpu_format="fc32"` — complex64, o mesmo que o
resto do cano usa. Ao contrário do `rtl_tcp` (que entrega uint8 cru e exige
normalização manual) e do `import-wav` (que reconstrói fase a partir de
áudio), aqui o UHD já devolve float complexo pronto: menos conversão, e
nenhuma das armadilhas de amplitude que as outras duas fontes carregam.
`otw_format="sc16"` é o formato NO FIO entre o USRP e o host — o UHD converte
de int16 para float32 sozinho; não é algo que este adapter precise refazer.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Iterator

import numpy as np
import zmq

from iq_recorder.domain.models import IqBlock

logger = logging.getLogger(__name__)

DEFAULT_BLOCK_SAMPLES = 8192
DEFAULT_RECV_TIMEOUT_S = 0.5  # bem maior que o default do UHD (0,1s): dá
# folga para o laço de tune não competir por CPU sem custar responsividade.

TUNE_TOPIC = b"tune"
DEFAULT_TUNE_RECEIVE_TIMEOUT_MS = 500


class UsrpConnectionError(RuntimeError):
    """`uhd` não está instalado, ou o USRP não respondeu."""


def _import_uhd():
    """Import tardio, de propósito.

    `uhd` só tem wheel para Windows no PyPI — dentro de container Linux, a
    instalação é via apt (`python3-uhd`), e só existe na imagem
    `Dockerfile.usrp`, não na imagem principal deste serviço. Um `import uhd`
    no topo do arquivo quebraria `record`, `replay`, `inspect` e
    `import-wav` — que não precisam de USRP nenhum — só porque o módulo
    existe no código-fonte.
    """
    try:
        import uhd
    except ImportError as error:
        raise UsrpConnectionError(
            "módulo 'uhd' não encontrado. Isto só roda dentro da imagem "
            "Dockerfile.usrp (Debian + python3-uhd) — a imagem principal do "
            "recorder não o inclui de propósito, para não pesar quem só "
            "grava, reproduz ou inspeciona."
        ) from error

    return uhd


class _TuneListener(threading.Thread):
    """Assina `:5557` e traduz cada `tune` num `set_rx_freq`.

    Mesmo papel do `_TuneListener` do `RtlTcpIqSource`, mesmo contrato de
    mensagem — é por isso que este adapter também aparece, do ponto de vista
    do Station Manager, como qualquer outra fonte sintonizável.
    """

    def __init__(self, address: str, usrp, channel: int, lock: threading.Lock, uhd_module) -> None:
        super().__init__(daemon=True, name="usrp-tune-listener")
        self._address = address
        self._usrp = usrp
        self._channel = channel
        self._lock = lock
        self._uhd = uhd_module
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, TUNE_TOPIC)
        self._socket.setsockopt(zmq.RCVTIMEO, DEFAULT_TUNE_RECEIVE_TIMEOUT_MS)
        self._socket.connect(address)
        self._stopping = threading.Event()

    def run(self) -> None:
        logger.info("Ouvindo tune em %s", self._address)

        while not self._stopping.is_set():
            try:
                frames = self._socket.recv_multipart()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                break

            if len(frames) != 2:
                continue

            try:
                frequency_hz = float(frames[1].decode())
            except (ValueError, UnicodeDecodeError):
                logger.warning("tune ilegível: %r", frames[1])
                continue

            with self._lock:
                try:
                    self._usrp.set_rx_freq(
                        self._uhd.types.TuneRequest(frequency_hz), self._channel
                    )
                except Exception:
                    logger.exception("Falha ao aplicar set_rx_freq")
                    continue

            logger.info("tune -> %.4f MHz", frequency_hz / 1e6)

        self._socket.close()
        self._context.term()

    def stop(self) -> None:
        self._stopping.set()


class UsrpIqSource:
    """Implementa IqStreamSource conectando num USRP via UHD.

    IQ real, capturado pelo rádio — não reconstruído, não normalizado à mão.
    A conversão de amostra que este adapter faz é zero: `cpu_format="fc32"`
    já entrega complex64 pronto.
    """

    def __init__(
        self,
        host: str,
        sample_rate_hz: float,
        frequency_hz: float,
        gain_db: float | None = None,
        channel: int = 0,
        tune_address: str | None = None,
        block_samples: int = DEFAULT_BLOCK_SAMPLES,
        recv_timeout_s: float = DEFAULT_RECV_TIMEOUT_S,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz precisa ser positivo, veio {sample_rate_hz}")
        if frequency_hz <= 0:
            raise ValueError(f"frequency_hz precisa ser positivo, veio {frequency_hz}")
        if block_samples <= 0:
            raise ValueError("block_samples precisa ser positivo")

        uhd = _import_uhd()
        self._uhd = uhd

        self.host = host
        self.sample_rate_hz = sample_rate_hz
        self.frequency_hz = frequency_hz
        self.gain_db = gain_db
        self.channel = channel
        self._block_samples = block_samples
        self._recv_timeout_s = recv_timeout_s
        self._send_lock = threading.Lock()

        # device args: "addr=<ip>" é o formato confirmado (constructor
        # MultiUSRP(args: str)). Falha em menos de 1 s se não houver
        # dispositivo — não precisamos de timeout próprio aqui, o UHD já tem
        # o dele.
        try:
            self._usrp = uhd.usrp.MultiUSRP(f"addr={host}")
        except RuntimeError as error:
            raise UsrpConnectionError(f"não consegui conectar em {host}: {error}") from error

        self._usrp.set_rx_rate(sample_rate_hz, channel)
        self._usrp.set_rx_freq(uhd.types.TuneRequest(frequency_hz), channel)
        if gain_db is not None:
            self._usrp.set_rx_gain(gain_db, channel)

        stream_args = uhd.usrp.StreamArgs("fc32", "sc16")
        stream_args.channels = [channel]
        self._streamer = self._usrp.get_rx_stream(stream_args)

        self._metadata = uhd.types.RXMetadata()
        # (1, block_samples): a forma usada no exemplo oficial do UHD
        # (recv_buffer indexado por canal) — não simplifiquei para 1D sem
        # confirmação real de que o recv() aceita, porque não há hardware
        # para testar a alternativa.
        self._buffer = np.zeros((1, block_samples), dtype=np.complex64)

        start_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        start_cmd.stream_now = True
        self._streamer.issue_stream_cmd(start_cmd)

        self._tuner: _TuneListener | None = None
        if tune_address:
            self._tuner = _TuneListener(tune_address, self._usrp, channel, self._send_lock, uhd)
            self._tuner.start()

        self._sequence = 0
        self._closed = False

    # --- IqStreamSource -------------------------------------------------------

    def blocks(self) -> Iterator[IqBlock]:
        """Lotes de IQ, indefinidamente, até `close()`.

        `recv()` tem timeout próprio (passado explicitamente, maior que o
        padrão do UHD) — por isso este laço reage a `close()` sem precisar de
        um segundo mecanismo de cancelamento, do mesmo padrão do
        `ZmqIqSource`.
        """
        error_code = self._uhd.types.RXMetadataErrorCode

        while not self._closed:
            n = self._streamer.recv(self._buffer, self._metadata, self._recv_timeout_s)

            if self._metadata.error_code == error_code.timeout:
                continue  # nada chegou nesta janela; volta e confere _closed
            if self._metadata.error_code == error_code.overflow:
                logger.warning("USRP: estouro de buffer — amostras perdidas")
                continue
            if self._metadata.error_code != error_code.none:
                logger.warning("USRP: %s", self._metadata.strerror())
                continue
            if n == 0:
                continue

            yield IqBlock(
                data=self._buffer[0, :n].copy().tobytes(),
                sequence=self._sequence,
                received_at=datetime.now(timezone.utc),
            )
            self._sequence += 1

    def close(self) -> None:
        self._closed = True

        if self._tuner is not None:
            self._tuner.stop()

        try:
            stop_cmd = self._uhd.types.StreamCMD(self._uhd.types.StreamMode.stop_cont)
            self._streamer.issue_stream_cmd(stop_cmd)
        except Exception:
            logger.exception("Falha ao emitir stop_cont")

        # MultiUSRP não tem um close()/disconnect() explícito na API
        # inspecionada — a conexão é liberada quando o objeto é coletado.
        # Soltar a referência aqui é o que dá para fazer sem hardware para
        # confirmar mais nada.
        self._streamer = None
        self._usrp = None
