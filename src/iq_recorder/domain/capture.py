"""O contrato de captura, versionado — SigMF.

Este módulo É o contrato (A4): a versão, os nomes dos campos e a função pura
que monta o sidecar. Nenhuma linha aqui abre arquivo ou socket; quem escreve
em disco é o FileIqSink (C2), e ele escreve o que esta função devolve.

POR QUE SIGMF, E NÃO UM FORMATO NOSSO. Um `.iq` cru mais um `.json` inventado
aqui dentro daria o mesmo trabalho e serviria só a nós. SigMF é o formato que
o resto do mundo de SDR já lê — inspectrum, SigMF-Python, GNU Radio — então
uma captura desta estação abre numa ferramenta de terceiros sem conversão, e
uma captura de terceiros (a de uma passagem do FS-1 no SatNOGS, por exemplo)
entra no nosso harness de regressão sem adaptador.

O par de arquivos:
    <nome>.sigmf-data   as amostras, cruas, exatamente como saíram do socket
    <nome>.sigmf-meta   este JSON

Regra que não se quebra: o `.sigmf-data` é byte a byte o que veio do PUB de
IQ. Nenhuma normalização, nenhum reordenamento, nenhum cabeçalho. É essa
regra que faz o replay ser indistinguível do vivo — e é ela que o teste de
round-trip do E3 verifica.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from iq_recorder.domain.models import CaptureProfile, FrequencySource

# Versão do núcleo do SigMF que declaramos. 1.0.0 é a que toda ferramenta lê;
# as versões posteriores acrescentam campos que não usamos aqui. Subir esta
# constante é mudar o contrato — e exige olhar quem consome as capturas
# antigas antes.
SIGMF_VERSION = "1.0.0"

# Versão do NOSSO perfil de uso do SigMF: quais campos a estação promete
# preencher e o que eles significam. É independente da versão do SigMF — o
# formato pode ficar parado enquanto a nossa convenção evolui, e é esta que o
# índice guarda para saber ler uma captura de seis meses atrás.
CAPTURE_CONTRACT_VERSION = "1.0.0"

DATA_SUFFIX = ".sigmf-data"
META_SUFFIX = ".sigmf-meta"

# Namespace das extensões próprias. O SigMF manda prefixar campos não-padrão,
# e o prefixo é o que impede que um `frequency_source` nosso colida com um
# campo que o SigMF venha a definir com esse nome.
GRS_NAMESPACE = "grs"


def _isoformat_utc(moment: datetime) -> str:
    """Timestamp no formato que o SigMF exige: ISO-8601 em UTC, sufixo Z.

    Um datetime ingênuo (sem tzinfo) é tratado como UTC em vez de rejeitado:
    o relógio da estação corre em UTC, e recusar a gravação de uma passagem
    porque faltou um tzinfo seria perder a passagem por uma questão de tipo.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_sigmf_metadata(
    profile: CaptureProfile,
    started_at: datetime,
    sample_count: int,
    sha512: str | None = None,
    recorder: str = "grs-iq-recorder",
) -> dict[str, Any]:
    """Monta o conteúdo do `.sigmf-meta` para uma captura.

    Função pura: mesma entrada, mesma saída, sem relógio nem disco. É o que
    permite ao teste do contrato comparar campo a campo sem gravar nada.

    :param profile: o CaptureProfile sob o qual a captura foi feita.
    :param started_at: instante do primeiro lote, no relógio da estação.
    :param sample_count: total de amostras COMPLETAS no `.sigmf-data`.
    :param sha512: hash do `.sigmf-data` fechado; None enquanto ele está
        aberto (o sidecar só ganha o campo quando o arquivo para de crescer).
    :param recorder: quem gravou, para rastrear regressão entre versões.

    :return: o dicionário a serializar como JSON.
    """
    if sample_count < 0:
        raise ValueError(f"sample_count não pode ser negativo: {sample_count}")

    global_block: dict[str, Any] = {
        "core:datatype": profile.datatype.value,
        "core:sample_rate": float(profile.sample_rate_hz),
        "core:version": SIGMF_VERSION,
        "core:recorder": recorder,
        "core:description": profile.description,
        # Campos nossos, sob o namespace grs: o SigMF não tem onde guardar de
        # qual perfil a captura saiu, nem como a frequência foi determinada —
        # e sem isso a captura deixa de ser auto-descritiva.
        f"{GRS_NAMESPACE}:contract_version": CAPTURE_CONTRACT_VERSION,
        f"{GRS_NAMESPACE}:profile": profile.name,
        f"{GRS_NAMESPACE}:frequency_source": profile.frequency_source.value,
    }

    if profile.rf_path:
        global_block[f"{GRS_NAMESPACE}:rf_path"] = profile.rf_path

    # Ganho automático é AUSÊNCIA de campo, não zero. Gravar `gain: 0` diria
    # "ganho de 0 dB", que é uma afirmação sobre o hardware — e falsa, porque
    # com AGC ligado ninguém sabe qual ganho o tuner escolheu.
    if profile.gain_db is not None:
        global_block[f"{GRS_NAMESPACE}:gain_db"] = float(profile.gain_db)

    if sha512 is not None:
        global_block["core:sha512"] = sha512

    # Um único segmento de captura: a sintonia é fixa nesta fatia, então a
    # frequência não muda do começo ao fim. Quando o Doppler entrar
    # (FrequencySource.TUNE_TOPIC), cada retune vira um segmento novo com o
    # seu `core:sample_start` — o formato já prevê isso, e é por isso que
    # `captures` é uma lista mesmo tendo um item só hoje.
    captures_block: list[dict[str, Any]] = [
        {
            "core:sample_start": 0,
            "core:frequency": float(profile.center_frequency_hz),
            "core:datetime": _isoformat_utc(started_at),
        }
    ]

    return {
        "global": global_block,
        "captures": captures_block,
        # Vazio de propósito. As anotações chegam depois da gravação: o pico
        # de PSD (D1) e as fronteiras de frame que o detector de syncword
        # encontrou (D2). Uma captura recém-fechada ainda não sabe nada disso.
        "annotations": [],
    }


def expected_data_bytes(profile: CaptureProfile, sample_count: int) -> int:
    """Tamanho que o `.sigmf-data` deve ter para `sample_count` amostras.

    Usado pela conferência do round-trip: um `.sigmf-data` cujo tamanho não é
    múltiplo exato disto é sinal de lote truncado na origem — o envelope do
    `grs-iq-rx` cortando uma amostra ao meio — e não de erro de gravação.
    """
    return sample_count * profile.datatype.bytes_per_sample


def is_frequency_annotated(profile: CaptureProfile) -> bool:
    """A frequência do sidecar é observada, ou só declarada?

    FIXED significa declarada: ninguém conferiu no rádio, é o que o perfil
    mandou sintonizar. TUNE_TOPIC significa observada — veio do que o
    `frequency-synthesizer` efetivamente publicou. A diferença importa na hora
    de depurar uma captura que não demodula: com FIXED, "a frequência está
    errada" continua sendo uma hipótese viva.
    """
    return profile.frequency_source is FrequencySource.TUNE_TOPIC
