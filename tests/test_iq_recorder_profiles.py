"""Testes dos perfis de captura (A3) e da configuração.

O que estes testes protegem é a distinção entre PERFIL e AMBIENTE: o que
descreve o enlace é versionado no código, e o que muda de máquina vem do
.env. Confundir os dois é como uma captura deixa de ser auto-descritiva.
"""

from __future__ import annotations

import pytest

from iq_recorder.config import DEFAULT_PROFILE, load_config
from iq_recorder.domain.models import CaptureProfile, FrequencySource, SampleFormat
from iq_recorder.domain.profiles import GRS_RX_FS2, get_profile, list_profiles


def test_perfil_do_fs2_esta_registrado() -> None:
    assert get_profile("grs-rx-fs2") is GRS_RX_FS2


def test_perfil_desconhecido_lista_os_conhecidos() -> None:
    """A mensagem tem de dizer o que existe: quem errou o nome no .env não
    consegue adivinhar o certo a partir de um KeyError seco."""
    with pytest.raises(KeyError, match="grs-rx-fs2"):
        get_profile("nao-existe")


def test_fs2_grava_em_cf32_le() -> None:
    assert GRS_RX_FS2.datatype is SampleFormat.CF32_LE


def test_fs2_usa_sintonia_fixa_enquanto_nao_ha_doppler() -> None:
    assert GRS_RX_FS2.frequency_source is FrequencySource.FIXED


def test_fs2_usa_ganho_automatico() -> None:
    assert GRS_RX_FS2.gain_db is None


def test_taxa_do_fs2_e_valida_no_rtl_sdr() -> None:
    """O RTL-SDR só aceita 225001–300000 e 900001–3200000 S/s. Fora disso o
    driver não dá erro — ele entrega outra taxa, em silêncio, e a captura sai
    com o sample_rate errado no sidecar."""
    taxa = GRS_RX_FS2.sample_rate_hz

    assert 225_001 <= taxa <= 300_000 or 900_001 <= taxa <= 3_200_000


def test_taxa_do_fs2_da_numero_inteiro_de_amostras_por_simbolo() -> None:
    """A 4800 baud (valor a confirmar — §4, item 4). Resto aqui faz o
    sincronismo de tempo começar com erro de fase por arredondamento."""
    baud_presumido = 4800

    assert GRS_RX_FS2.sample_rate_hz % baud_presumido == 0


def test_vazao_da_captura() -> None:
    """240 kS/s x 8 B = 1,92 MB/s. O número que dimensiona disco antes de uma
    campanha — dez minutos de passagem são ~1,15 GB."""
    assert GRS_RX_FS2.bytes_per_second == pytest.approx(1_920_000.0)


def test_perfil_exige_frequencia_positiva() -> None:
    with pytest.raises(ValueError, match="frequência"):
        CaptureProfile(
            name="ruim", description="", center_frequency_hz=0.0, sample_rate_hz=240_000.0
        )


def test_perfil_exige_taxa_positiva() -> None:
    with pytest.raises(ValueError, match="taxa"):
        CaptureProfile(
            name="ruim", description="", center_frequency_hz=145e6, sample_rate_hz=-1.0
        )


def test_perfil_exige_nome() -> None:
    with pytest.raises(ValueError, match="nome"):
        CaptureProfile(
            name="", description="", center_frequency_hz=145e6, sample_rate_hz=240_000.0
        )


def test_perfil_e_imutavel() -> None:
    """Frozen de propósito: uma captura em andamento não pode ter o perfil
    trocado debaixo dela."""
    with pytest.raises(Exception):
        GRS_RX_FS2.center_frequency_hz = 437e6  # type: ignore[misc]


def test_listagem_e_estavel() -> None:
    assert [p.name for p in list_profiles()] == sorted(p.name for p in list_profiles())


def test_config_padrao_usa_o_perfil_do_fs2() -> None:
    config = load_config(env={})

    assert config.profile is GRS_RX_FS2
    assert DEFAULT_PROFILE == "grs-rx-fs2"


def test_config_conecta_no_grs_iq_rx_por_padrao() -> None:
    """O grs-iq-rx BINDA a :5556; todo mundo mais conecta nela."""
    config = load_config(env={})

    assert config.iq_source_address == "tcp://grs-iq-rx:5556"


def test_replay_binda_em_vez_de_conectar() -> None:
    """No replay o gravador OCUPA o lugar do grs-iq-rx — por isso bind, e por
    isso o grs-iq-rx tem de estar desligado."""
    config = load_config(env={})

    assert config.iq_publish_address.startswith("tcp://*")


def test_config_sem_banco_e_valida() -> None:
    """Sem índice o serviço grava do mesmo jeito: a captura é o artefato."""
    config = load_config(env={})

    assert config.database_url is None


def test_perfil_invalido_no_ambiente_derruba_o_boot() -> None:
    with pytest.raises(KeyError):
        load_config(env={"RECORDER_PROFILE": "datilografado-errado"})


def test_limite_de_gravacao_nao_numerico_derruba_o_boot() -> None:
    with pytest.raises(ValueError, match="inteiro"):
        load_config(env={"RECORDER_MAX_CAPTURE_SECONDS": "um minuto"})


def test_limite_de_gravacao_zero_derruba_o_boot() -> None:
    with pytest.raises(ValueError, match="positivo"):
        load_config(env={"RECORDER_MAX_CAPTURE_SECONDS": "0"})
