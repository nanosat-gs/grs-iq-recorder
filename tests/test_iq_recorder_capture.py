"""Testes do contrato de captura (A4).

Confere o que docs/capture-contract.md promete, campo a campo. O contrato é
uma função pura, então estes testes não gravam arquivo nem abrem socket — e é
isso que os torna a rede de segurança de quem for mexer no FileIqSink (C2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from iq_recorder.domain.capture import (
    CAPTURE_CONTRACT_VERSION,
    SIGMF_VERSION,
    build_sigmf_metadata,
    expected_data_bytes,
    is_frequency_annotated,
)
from iq_recorder.domain.models import CaptureProfile, FrequencySource, SampleFormat

STARTED_AT = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def profile() -> CaptureProfile:
    return CaptureProfile(
        name="teste",
        description="perfil de teste",
        center_frequency_hz=145_900_000.0,
        sample_rate_hz=240_000.0,
        rf_path="antena -> SDR",
    )


def test_campos_obrigatorios_do_sigmf(profile: CaptureProfile) -> None:
    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=1000)

    assert meta["global"]["core:datatype"] == "cf32_le"
    assert meta["global"]["core:sample_rate"] == 240_000.0
    assert meta["global"]["core:version"] == SIGMF_VERSION
    assert meta["captures"][0]["core:frequency"] == 145_900_000.0
    assert meta["captures"][0]["core:sample_start"] == 0


def test_versao_do_contrato_proprio_e_separada_da_do_sigmf(profile: CaptureProfile) -> None:
    """As duas versões são independentes — ver docs/capture-contract.md."""
    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=1)

    assert meta["global"]["grs:contract_version"] == CAPTURE_CONTRACT_VERSION
    assert meta["global"]["core:version"] == SIGMF_VERSION


def test_datetime_sai_em_utc_com_sufixo_z(profile: CaptureProfile) -> None:
    """O SigMF exige ISO-8601 em UTC. Um offset de fuso no arquivo quebraria
    a leitura em ferramenta de terceiros."""
    em_brasilia = STARTED_AT.astimezone(timezone(timedelta(hours=-3)))

    meta = build_sigmf_metadata(profile, em_brasilia, sample_count=1)

    assert meta["captures"][0]["core:datetime"] == "2026-09-21T12:00:00Z"


def test_datetime_ingenuo_e_tratado_como_utc(profile: CaptureProfile) -> None:
    """Faltar tzinfo não pode custar uma passagem — ver capture.py."""
    ingenuo = datetime(2026, 9, 21, 12, 0, 0)

    meta = build_sigmf_metadata(profile, ingenuo, sample_count=1)

    assert meta["captures"][0]["core:datetime"] == "2026-09-21T12:00:00Z"


def test_sem_hash_enquanto_o_arquivo_nao_fechou(profile: CaptureProfile) -> None:
    """Sidecar sem core:sha512 descreve captura em andamento, ou interrompida."""
    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=10)

    assert "core:sha512" not in meta["global"]


def test_hash_aparece_quando_informado(profile: CaptureProfile) -> None:
    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=10, sha512="abc123")

    assert meta["global"]["core:sha512"] == "abc123"


def test_ganho_automatico_e_ausencia_de_campo(profile: CaptureProfile) -> None:
    """`gain_db: 0` afirmaria 0 dB, que é falso quando o AGC está no comando."""
    assert profile.gain_db is None

    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=1)

    assert "grs:gain_db" not in meta["global"]


def test_ganho_fixo_vira_campo(profile: CaptureProfile) -> None:
    com_ganho = CaptureProfile(
        name=profile.name,
        description=profile.description,
        center_frequency_hz=profile.center_frequency_hz,
        sample_rate_hz=profile.sample_rate_hz,
        gain_db=28.0,
    )

    meta = build_sigmf_metadata(com_ganho, STARTED_AT, sample_count=1)

    assert meta["global"]["grs:gain_db"] == 28.0


def test_um_unico_segmento_de_captura_com_sintonia_fixa(profile: CaptureProfile) -> None:
    """A lista existe para o Doppler futuro, mas hoje tem um item só."""
    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=1)

    assert len(meta["captures"]) == 1


def test_anotacoes_nascem_vazias(profile: CaptureProfile) -> None:
    meta = build_sigmf_metadata(profile, STARTED_AT, sample_count=1)

    assert meta["annotations"] == []


def test_sample_count_negativo_e_recusado(profile: CaptureProfile) -> None:
    with pytest.raises(ValueError, match="sample_count"):
        build_sigmf_metadata(profile, STARTED_AT, sample_count=-1)


def test_tamanho_esperado_do_arquivo_de_amostras(profile: CaptureProfile) -> None:
    """8 bytes por amostra: float32 I + float32 Q."""
    assert expected_data_bytes(profile, 1000) == 8000


def test_frequencia_fixa_e_declarada_nao_observada(profile: CaptureProfile) -> None:
    assert profile.frequency_source is FrequencySource.FIXED
    assert is_frequency_annotated(profile) is False


def test_frequencia_do_tune_e_observada(profile: CaptureProfile) -> None:
    com_doppler = CaptureProfile(
        name=profile.name,
        description=profile.description,
        center_frequency_hz=profile.center_frequency_hz,
        sample_rate_hz=profile.sample_rate_hz,
        frequency_source=FrequencySource.TUNE_TOPIC,
    )

    assert is_frequency_annotated(com_doppler) is True


def test_cf32_le_tem_oito_bytes_por_amostra() -> None:
    """O acordo entre grs-iq-rx (struct de dois float) e grs-demodulator
    (np.frombuffer complex64). Mudar isto quebra as duas pontas."""
    assert SampleFormat.CF32_LE.bytes_per_sample == 8
