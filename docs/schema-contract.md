# Contrato de schema do gravador

Este documento é normativo. Quem o implementa são
[`postgres_capture_index.py`](../src/iq_recorder/adapters/postgres_capture_index.py)
(índice de capturas) e
[`postgres_packet_archive.py`](../src/iq_recorder/adapters/postgres_packet_archive.py)
(arquivo de raw packets), e [`schema_check.py`](../src/iq_recorder/schema_check.py)
o confere no boot.

## Esta tabela é nossa, e isso muda tudo

O TC Scheduler tem um documento com o mesmo nome, mas a situação lá é o
oposto: o schema dele é propriedade do **TC Generator**, e o contrato existe
para tornar visível uma dependência entre dois repositórios que nada declara.

Aqui a tabela é do gravador. Ninguém mais a define, ninguém mais a escreve.
Isso traz uma consequência prática boa: o serviço a cria no boot com
`CREATE SCHEMA/TABLE IF NOT EXISTS`, e **a armadilha do
`docker-entrypoint-initdb.d` some do caminho**. Aquele diretório só roda em
volume vazio, então tudo que depende dele exige um `down -v` — que apaga os
dados — para mudar de forma. Uma estação que já tem meses de telecomandos
ganha o índice de capturas sem perder nada.

Ela vive em **`mission_control`**, e não no `public` do TC Generator. Separar
custa nada agora e evita que uma captura e um telecomando disputem um nome de
tabela daqui a um ano.

## `mission_control.iq_captures` — append-only

| Coluna | Tipo | O que é |
|---|---|---|
| `capture_id` | `TEXT PRIMARY KEY` | nome da captura; é também o nome dos arquivos |
| `profile_name` | `TEXT` | qual `CaptureProfile` regeu a gravação |
| `center_frequency_hz` | `DOUBLE PRECISION` | frequência sintonizada |
| `sample_rate_hz` | `DOUBLE PRECISION` | taxa de amostragem |
| `datatype` | `TEXT` | `cf32_le`, no vocabulário do SigMF |
| `started_at` | `TIMESTAMPTZ` | primeiro lote que chegou |
| `ended_at` | `TIMESTAMPTZ` | último lote |
| `sample_count` | `BIGINT` | amostras **completas** |
| `sha512` | `TEXT` | do `.sigmf-data` |
| `data_path` | `TEXT` | onde o arquivo está |
| `indexed_at` | `TIMESTAMPTZ` | quando a linha entrou |

Um índice em `started_at DESC`: buscar por instante é a consulta natural
("o que foi gravado naquela passagem?"), e é a única que justifica um índice
a esta altura.

### Append-only é regra, não convenção

Não há `UPDATE` nem `DELETE` neste serviço. Uma captura é uma **observação** —
ela aconteceu, num instante, com uma configuração. Reescrever a linha depois é
reescrever o que a estação viu, e é assim que uma regressão de DSP fica
impossível de reproduzir: o arquivo diz uma coisa, o índice diz outra, e não há
como saber qual dos dois envelheceu.

### `sha512`, e não `sha256`

Porque o campo do SigMF é `core:sha512`. Guardar `sha256` aqui daria duas somas
para a mesma captura, e a do índice não bateria com a do sidecar — que é
justamente a conferência que interessa.

E é o hash do **`.sigmf-data`**, nunca do sidecar: o sidecar ganha anotação
depois da gravação (o PSD do D1, a contagem de frames do D2), enquanto as
amostras são imutáveis por definição. Hash de coisa que muda não verifica nada.

## `mission_control.raw_packets` — append-only

Escrita pelo `archive-packets` (no compose: `grs-packet-archiver` e
`grs-packet-archiver-uhf`, um por rádio). Cada linha é um raw packet como o
detector de syncword o publicou: uma observação, não telemetria. A mesma regra
da tabela de capturas: sem UPDATE, sem DELETE.

| Coluna | Tipo | O que é |
|---|---|---|
| `id` | `BIGSERIAL PRIMARY KEY` | |
| `received_at` | `TIMESTAMPTZ NOT NULL` | relógio da estação ao chegar ao arquivador (µs) |
| `detected_at` | `TIMESTAMPTZ` | relógio do detector, resolução de 1 s |
| `detector_seq`, `bit_offset` | `BIGINT` | numeração do detector — recomeça quando ele reinicia |
| `syncword` | `TEXT` | o syncword procurado |
| `max_sync_errors` | `INTEGER` | a tolerância em vigor, não a distância medida |
| `payload` | `BYTEA NOT NULL` | os 255 bytes depois do syncword, crus |
| `payload_sha256` | `TEXT NOT NULL` | para agrupar e achar repetidos; **não** é único (replay gera cópias legítimas) |
| `header` | `JSONB NOT NULL` | o cabeçalho do detector, inteiro |
| `archiver_run` | `UUID NOT NULL` | uma por processo do arquivador: separa sessões |
| `archived_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `radio` | `TEXT` | de que cadeia veio (`vhf`, `uhf`), do `RECORDER_RADIO` do arquivador; `NULL` nas linhas de antes de haver dois rádios |

Índices em `received_at DESC` e `payload_sha256`. A coluna `radio` entrou
depois da tabela: bancos antigos a ganham no boot com
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

## O que o `schema_check` pega

Como a tabela nasce de `ensure_schema()`, a divergência só aparece de dois
jeitos:

1. **Alguém alterou a tabela à mão** no banco, e o serviço passa a escrever
   numa coluna que sumiu.
2. **Uma versão mais nova do serviço subiu contra um banco criado por uma
   antiga.** `CREATE TABLE IF NOT EXISTS` **não adiciona coluna** a uma tabela
   que já existe — é preciso `ALTER TABLE` à mão.

O segundo é o silencioso, e o mais provável. Sem a conferência ele aparece como
um erro de SQL no fim de uma gravação — depois de a passagem ter acontecido e o
arquivo já estar em disco, quando não há mais o que fazer.

**Loga e segue, nunca levanta.** A captura é o artefato; o índice é
conveniência. Derrubar o gravador por causa de uma coluna faltando trocaria um
problema pequeno (uma linha que não entra) por um grande (a passagem perdida).
É o mesmo argumento que faz o serviço gravar normalmente sem
`PG_DATABASE_URL`.

## Mudando a tabela

Acrescentar coluna a uma instalação que já existe:

```sql
ALTER TABLE mission_control.iq_captures ADD COLUMN nova_coluna TEXT;
```

E acrescentá-la a `REQUIRED_COLUMNS` do adapter da tabela
(`postgres_capture_index.py` ou `postgres_packet_archive.py`), senão o
`schema_check` não a confere e a divergência volta a ser silenciosa. Melhor
ainda, como fez a `raw_packets.radio`: um `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` na lista de DDL do adapter, e a instalação existente se ajusta sozinha
no boot.
