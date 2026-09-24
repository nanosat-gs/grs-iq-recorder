# GRS IQ Recorder

Captura, replay e índice do fluxo de IQ da estação terrestre SpaceLab.

É o único bloco da metade de RF escrito pela equipe. Os outros três
(`grs-iq-rx`, `grs-demodulator`, `grs-syncword-detector`) são adotados do
`spacelab-ufsc`; este nasce aqui porque não existe lá.

## Para que serve

Gravando o IQ — banda-base complexa, **antes** do demodulador — a passagem
vira artefato, e o artefato vira teste.

```
SDR -> grs-iq-rx -- PUB :5556 --+--> grs-demodulator -> syncword -> raw packets
                                |
                                +--(tap)--> grs-iq-recorder -> captura SigMF
                                                                    |
   replay: ZmqIqPublisher <- FileIqSource <----------------------- -+
   (republica na :5556; com o grs-iq-rx desligado, o resto do cano
    não tem como saber que do outro lado há um arquivo, e não um rádio)
```

O demodulador e o detector de syncword passam a ser exercitáveis offline,
determinísticos, sem SDR e sem esperar o satélite passar de novo. É essa a
razão de o gravador existir — não é ferramenta de diagnóstico, é a espinha de
regressão de toda a metade de RF.

## Estado

**Grava e reproduz.** Épicos A e C fechados.

| Feito | O quê |
|---|---|
| A2 | esqueleto hexagonal — portas, modelos, config, boot |
| A3 | `CaptureProfile` e a instância `grs-rx-fs2` |
| A4 | contrato de captura versionado (SigMF) — `docs/capture-contract.md` |
| C1 | `ZmqIqSource` — tap ao vivo no PUB :5556 |
| C2 | `FileIqSink` — grava o par SigMF e o hash |
| C3 | `FileIqSource` + `ZmqIqPublisher` — replay |
| C4 | `PostgresCaptureIndex` + `schema_check` no boot |

| D1 | `NumpyPsdView` — PSD, resumo e `inspect` |

## Usando

```bash
# grava 30 s do que estiver publicando IQ
python -m iq_recorder.main record --seconds 30

# reproduz uma captura no mesmo tópico
python -m iq_recorder.main replay /app/captures/passagem-teste

# tem sinal nessa captura, e onde?
python -m iq_recorder.main inspect /app/captures/passagem-teste

# traz um WAV do gqrx para dentro do cano
python -m iq_recorder.main import-wav beacon.wav --baud 1200 --frequency 145900000
```

O `inspect` sai com código 2 quando a captura parece ruído — dá para usá-lo
como portão antes de gastar uma tarde depurando DSP.

No replay o publicador **BINDA** a :5556: o `grs-iq-rx` (ou o `grs-sdr-sim`)
tem de estar desligado, ou os dois disputam a porta e quem perde cai em
silêncio.

## Desenho

Hexagonal, como os outros blocos da estação. As portas estão em
`src/iq_recorder/domain/ports.py`, e o desenho inteiro cabe numa observação:

> `ZmqIqSource` (o tap ao vivo) e `FileIqSource` (o replay) implementam a
> **mesma porta**.

Gravar de um rádio e reproduzir de um arquivo são o mesmo laço com adapters
diferentes. É isso que faz o teste ponta a ponta rodar sem hardware.

## Contrato de captura

**SigMF**, e o documento normativo é [docs/capture-contract.md](docs/capture-contract.md).
O par de arquivos é `<nome>.sigmf-data` (amostras) + `<nome>.sigmf-meta` (JSON).

A regra que não se quebra: **o `.sigmf-data` é byte a byte o que veio do PUB
de IQ.** Sem normalização, sem cabeçalho, sem conversão. Se o gravador
transformasse as amostras na entrada, o replay republicaria algo que o rádio
nunca publicou, e o demodulador estaria sendo testado contra um sinal
inventado.

## Rodando

```bash
pip install -e .[dev]
python -m iq_recorder.main
pytest -q
```

Em produção sobe pelo compose do `nanosat-gs/grs-station`, no profile `rx`.

## Configuração

Ver [.env.example](.env.example). A fronteira que importa: o que descreve o
**enlace** (frequência, taxa, formato) é versionado em
`src/iq_recorder/domain/profiles.py`; o que muda de **máquina** (endereço do
socket, diretório, limite) vem do ambiente. Se a frequência desse para
sobrescrever pelo `.env`, a captura passaria a depender de qual era o `.env`
naquele dia — que é exatamente o que o `CaptureProfile` existe para evitar.

## Licença

Código próprio da equipe. Os blocos adotados de RF são GPL v3 e rodam como
processos separados falando ZMQ — ver `docs/rx-datapath.md` no `grs-station`.
