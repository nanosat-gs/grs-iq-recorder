"""Perfis de captura registrados — A3.

Um perfil por configuração de recepção que a estação sabe gravar. Hoje há um
só, e ele é real: o downlink do FS-2.

Perfil é dado, não configuração de ambiente. O que muda de máquina para
máquina (qual dongle, qual diretório) vive no .env; o que descreve o ENLACE
(o que sintonizar, a que taxa, por qual caminho de RF) vive aqui, versionado
junto com o código, porque é isso que uma captura de seis meses atrás precisa
que ainda exista para ser relida.
"""

from __future__ import annotations

from iq_recorder.domain.models import CaptureProfile, FrequencySource, SampleFormat

# --------------------------------------------------------------------------
# ATENÇÃO: A FREQUÊNCIA ABAIXO É UM VALOR PROVISÓRIO.
#
# A modulação do FS-2 está confirmada no firmware do TTC2 (Si446x,
# MODEM_MOD_TYPE = 0x03 => 2GFSK) e o syncword do NGHam é BA 67 54 7E. O que
# NÃO está confirmado é a frequência de downlink nem o baud: dependem da
# coordenação IARU / do datasheet do TTC2, e é o item 4 do §4 do documento da
# fatia — a última incerteza de RF que sobrou.
#
# 145.9 MHz é a beacon do FS-1, e está aqui pelo mesmo motivo que as
# coordenadas GS_* da estação são São Paulo: para o cano subir e ser
# exercitado enquanto o número real não chega. Trocar ANTES de qualquer
# campanha de gravação de verdade — uma captura na frequência errada é ruído
# gravado com muito capricho.
# --------------------------------------------------------------------------
_FS2_DOWNLINK_HZ_PLACEHOLDER = 145_900_000.0

# 240 kS/s, e o número tem duas razões.
#
# 1. O RTL-SDR não aceita qualquer taxa: os intervalos válidos são
#    225001–300000 e 900001–3200000 S/s. Pedir 48 kS/s — que é a constante
#    DEMOD_DEFAULT_SAMPLE_RATE do `grs-demodulator` — não dá erro: o driver
#    simplesmente entrega outra coisa. Ver docs/rx-datapath.md.
# 2. 240000 / 4800 = 50 amostras por símbolo, exato. Sem resto, o
#    sincronismo de tempo (Mueller & Muller) não começa perdendo fase por
#    conta de arredondamento.
_GRS_RX_SAMPLE_RATE_HZ = 240_000.0

GRS_RX_FS2 = CaptureProfile(
    name="grs-rx-fs2",
    description="GRS-RX / downlink FS-2 — 2GFSK, NGHam, syncword BA 67 54 7E",
    center_frequency_hz=_FS2_DOWNLINK_HZ_PLACEHOLDER,
    sample_rate_hz=_GRS_RX_SAMPLE_RATE_HZ,
    datatype=SampleFormat.CF32_LE,
    # Ganho automático: o AGC do tuner é melhor do que um número fixo chutado
    # sem medir o caminho de RF real. Fixar ganho é coisa de depois da
    # primeira campanha, com medida na mão.
    gain_db=None,
    rf_path="antena -> RTL-SDR (grs-iq-rx) -> ZMQ PUB :5556",
    # FIXED porque a impl. C do `grs-iq-rx` não tem retune: ela sintoniza no
    # boot e fica. Vira TUNE_TOPIC quando o Doppler entrar, e aí a frequência
    # do sidecar passa a ser observada em vez de declarada.
    frequency_source=FrequencySource.FIXED,
)

_REGISTRY: dict[str, CaptureProfile] = {
    GRS_RX_FS2.name: GRS_RX_FS2,
}


def get_profile(name: str) -> CaptureProfile:
    """Busca um perfil pelo nome, ou explode dizendo quais existem.

    Erro alto e cedo de propósito: um nome de perfil errado no .env tem de
    derrubar o serviço no boot, e não virar uma captura gravada com a
    configuração de outro enlace.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(nenhum)"
        raise KeyError(f"perfil de captura desconhecido: {name!r}. Conhecidos: {known}") from None


def list_profiles() -> list[CaptureProfile]:
    """Todos os perfis registrados, em ordem estável de nome."""
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]
