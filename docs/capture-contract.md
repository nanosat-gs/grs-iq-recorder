# Contrato de captura — versão 1.0.0

Este documento é normativo. Quem o implementa é `src/iq_recorder/domain/capture.py`,
e `tests/test_iq_recorder_capture.py` confere campo a campo. Mudar um sem os
outros dois é como o formato passa a divergir em silêncio.

Duas versões, independentes:

| Versão | Constante | O que versiona |
|---|---|---|
| SigMF **1.0.0** | `SIGMF_VERSION` | o formato, que é de terceiros |
| Perfil GRS **1.0.0** | `CAPTURE_CONTRACT_VERSION` | o que a estação promete preencher |

Separadas porque evoluem por motivos diferentes: o SigMF pode ficar parado
anos enquanto a nossa convenção muda, e o índice precisa saber qual das duas
mudou para reler uma captura antiga.

## Os dois arquivos

```
<nome>.sigmf-data    amostras, cruas
<nome>.sigmf-meta    JSON, o descritor
```

### A regra que não se quebra

**O `.sigmf-data` é byte a byte o que veio do PUB de IQ.** Sem normalização,
sem reordenamento, sem cabeçalho, sem conversão de tipo.

Não é purismo. É o que sustenta o replay: se o gravador transformasse as
amostras na entrada, o replay republicaria algo diferente do que o rádio
publicou, e o demodulador estaria sendo exercitado contra um sinal que nunca
existiu. A regressão que isso esconde é a pior de todas — a que só aparece com
hardware real, depois de o teste offline passar em verde.

O teste de round-trip do E3 é exatamente esta regra: gravar e reproduzir tem
de devolver os mesmos bytes.

### Formato das amostras

`cf32_le`: `float32 I` seguido de `float32 Q`, intercalados, little-endian —
8 bytes por amostra, o `complex64` do numpy.

Não é escolha nossa, é o que as duas pontas já fazem: o `grs-iq-rx` publica
`struct { float i; float q; }` sem cabeçalho, e o `grs-demodulator` lê aquilo
com `np.frombuffer(buf, dtype=np.complex64)`. O contrato aqui apenas registra
o acordo que o código já tem.

## O sidecar

```json
{
  "global": {
    "core:datatype": "cf32_le",
    "core:sample_rate": 240000.0,
    "core:version": "1.0.0",
    "core:recorder": "grs-iq-recorder",
    "core:description": "GRS-RX / downlink FS-2 — 2GFSK, NGHam, syncword BA 67 54 7E",
    "core:sha512": "<hash do .sigmf-data fechado>",
    "grs:contract_version": "1.0.0",
    "grs:profile": "grs-rx-fs2",
    "grs:frequency_source": "fixed",
    "grs:rf_path": "antena -> RTL-SDR (grs-iq-rx) -> ZMQ PUB :5556"
  },
  "captures": [
    {
      "core:sample_start": 0,
      "core:frequency": 145900000.0,
      "core:datetime": "2026-09-21T12:00:00Z"
    }
  ],
  "annotations": []
}
```

### Campos, e por que cada um está aí

**`core:sha512`** — do arquivo de amostras, não do sidecar. O sidecar ganha
anotação depois (o PSD do D1, as fronteiras de frame do D2); as amostras são
imutáveis. Hash de coisa que muda não verifica nada. SHA-512 e não SHA-256
porque o campo do SigMF é esse — guardar sha256 no índice daria duas somas
para a mesma captura, e a do índice não bateria com a do sidecar.

O campo só aparece quando o arquivo fecha. Um `.sigmf-meta` sem `core:sha512`
descreve uma captura ainda em andamento, ou interrompida.

**`grs:*`** — o namespace próprio. O SigMF manda prefixar campo não-padrão, e
o prefixo é o que evita colisão se o SigMF um dia definir `frequency_source`.

**`grs:frequency_source`** — `fixed` ou `tune_topic`, e a diferença é entre
frequência **declarada** e **observada**:

- `fixed` — é o que o perfil mandou sintonizar. Ninguém conferiu no rádio. É
  o caso de hoje: a impl. C do `grs-iq-rx` não tem retune, sintoniza no boot e
  fica.
- `tune_topic` — veio do que o `frequency-synthesizer` publicou em
  `[b"tune", <freq Hz ASCII>]` na :5557. Caminho do Doppler, **fora desta
  fatia**.

A distinção existe para depuração: numa captura que não demodula, com `fixed`
a hipótese "a frequência estava errada" continua viva, e com `tune_topic` não.

**`grs:gain_db`** — **ausente** significa ganho automático. Escrever `0` diria
"0 dB", que é uma afirmação sobre o hardware, e falsa: com o AGC ligado
ninguém sabe qual ganho o tuner escolheu.

**`captures` é uma lista com um item só.** Com sintonia fixa a frequência não
muda do começo ao fim. Quando o Doppler entrar, cada retune vira um segmento
novo com o seu `core:sample_start` — o formato já prevê, e é por isso que a
lista existe desde já em vez de um objeto.

**`annotations` nasce vazio.** As anotações chegam depois da gravação: o pico
do PSD (D1) e as fronteiras de frame que o detector de syncword achou (D2).
Uma captura recém-fechada não sabe nada disso ainda.

## Compatibilidade

Dentro da mesma versão maior do perfil GRS:

- **pode** entrar campo novo sob `grs:` — quem lê e não conhece, ignora;
- **pode** entrar anotação nova em `annotations`;
- **não pode** mudar o significado de campo existente, nem sair campo, nem o
  `core:datatype` mudar. Qualquer um dos três sobe a versão maior.

Uma captura sem `grs:contract_version` é anterior a este documento e não deve
ser tratada como conforme.

## Ferramentas de terceiros

O formato foi escolhido para que a captura abra fora daqui sem conversão —
`inspectrum`, SigMF-Python, GNU Radio. O caminho inverso também vale, e é o
que interessa ao E2: uma captura de terceiros (uma passagem do FS-1 gravada
no SatNOGS, digamos) entra no harness de regressão sem adaptador.
