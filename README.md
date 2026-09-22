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

**Esqueleto (A2).** O serviço sobe, resolve o perfil de captura, imprime o
resumo do que gravaria, e fica de pé. **Ele ainda não grava.**

| Feito | O quê |
|---|---|
| A2 | esqueleto hexagonal — portas, modelos, config, boot |
| A3 | `CaptureProfile` e a instância `grs-rx-fs2` |
| A4 | contrato de captura versionado (SigMF) — `docs/capture-contract.md` |

| A fazer | O quê |
|---|---|
| C1 | `ZmqIqSource` — tap ao vivo no PUB :5556 |
| C2 | `FileIqSink` — grava o par SigMF e o hash |
| C3 | `FileIqSource` + `ZmqIqPublisher` — replay |
| C4 | `PostgresCaptureIndex` + `schema_check` no boot |
| D1 | `NumpyPsdView` — PSD e resumo |

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
