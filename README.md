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
# grava 30 s do que estiver publicando IQ, contando os pacotes que saem do detector
python -m iq_recorder.main record --seconds 30 --count-packets

# reproduz uma captura no mesmo tópico (no compose: serviço grs-iq-replay, abaixo)
python -m iq_recorder.main replay /app/captures/passagem-teste --count-packets

# tem sinal nessa captura, e onde?
python -m iq_recorder.main inspect /app/captures/passagem-teste

# traz um WAV do gqrx para dentro do cano
python -m iq_recorder.main import-wav beacon.wav --baud 1200 --frequency 145900000

# SDR real, via rede, sem GUI — republica um rtl_tcp na :5556
python -m iq_recorder.main bridge-rtltcp --rtltcp-host 192.168.1.50     --tune-source tcp://frequency-synthesizer:5557
```

O `inspect` sai com código 2 quando a captura parece ruído — dá para usá-lo
como portão antes de gastar uma tarde depurando DSP. Ele mostra o espectro;
a **contagem de quadros** vem de `--count-packets`, que assina a saída do
detector de syncword (`RECORDER_PACKETS_ADDRESS`) durante a gravação ou o
replay — o gravador não demodula nada, então conta do lado de fora, que é o
que prova o cano inteiro.

### Arquivo de raw packets no banco

`archive-packets` é um serviço (no compose: `grs-packet-archiver`) que assina
a saída do detector de syncword e grava cada raw packet em
`mission_control.raw_packets`, append-only. Sem ele, o que sai da :5558
some: PUB não guarda nada.

Cada linha: horário de recepção em solo (µs), `detected_at` do detector
(resolução de 1 s), `detector_seq`, `bit_offset`, syncword, tolerância, o
payload cru (255 bytes), o SHA-256 dele, o cabeçalho JSON inteiro e a sessão
do arquivador (`archiver_run` — a numeração do detector recomeça quando ele
reinicia). Não é telemetria: é o que a decodificação NGHam vai ler.

```bash
docker compose exec grs-packet-archiver python -m iq_recorder.main packets --limit 20
```

Se o banco cair, os pacotes ficam num buffer e são regravados quando ele
volta; o que passar do teto (10 mil) é contado e avisado no log. Medido com o
Postgres parado 15 s e o simulador transmitindo: voltou a gravar 3 s depois
de o banco subir, sequência sem buracos.

Replays também passam pelo detector e também são arquivados — são recepções
do cano. Pacotes publicados antes de o arquivador conectar (logo depois de
subir) se perdem, como em qualquer PUB/SUB: o arquivador tem de estar de pé
antes da passagem.

### Replay pelo cano (compose)

O replay **BINDA** a :5556 e precisa responder pelo nome da fonte ao vivo
(`grs-iq-rx`), porque é esse o nome que o demodulador assina. Por isso ele é
um serviço próprio, `grs-iq-replay`, e a fonte ao vivo tem de estar desligada:

```bash
docker compose stop grs-sdr-sim            # ou grs-iq-rx-usrp / grs-iq-rx
docker compose run --rm --use-aliases grs-iq-replay replay /app/captures/<nome> --count-packets
```

`--use-aliases` é obrigatório: sem ele o `run` não aplica o alias. Medido ao
vivo: uma gravação de 10 s contou 16 pacotes, e três replays dela contaram
16, 16 e 16.

O publicador do replay é XPUB e **espera o demodulador se inscrever** antes
de começar. Uma pausa fixa não bastava: com a fonte desligada, o demodulador
fica tentando resolver um nome que não existe, e a consulta de DNS trava a
thread do ZMQ por segundos — dois replays do mesmo arquivo chegaram a dar 13
e 9 pacotes.

O USRP N210 não passa por este repositório: ele tem receptor próprio no bloco
SDR (`grs-iq-rx`, pasta `usrp/`, python3-uhd, com painel de configuração).

### `bridge-rtltcp` — SDR real, sem gqrx, sem GUI

Conecta num [`rtl_tcp`](https://github.com/librtlsdr/librtlsdr) — o servidor
de linha de comando do mesmo projeto que o `grs-iq-rx` já usa — e republica o
IQ real (capturado, não reconstruído) na :5556. Só o `rtl_tcp` precisa de
acesso USB ao dongle; a máquina que roda `iq_recorder` pode ser qualquer
outra da rede.

```bash
# na máquina com o dongle, sem Docker:
rtl_tcp -a 0.0.0.0 -p 1234
```

Com `--tune-source`, assina o mesmo `tune` em `:5557` que o `grs-sdr-sim`
já consome — então o Station Manager comanda este receptor de verdade pelo
mesmo caminho já validado com o simulador, sem precisar do gqrx nem de
nenhum passo manual. Protocolo confirmado contra a fonte oficial do
`librtlsdr` (`src/rtl_tcp.c`), não deduzido — ver o cabeçalho de
`adapters/rtltcp_iq_source.py`.

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
