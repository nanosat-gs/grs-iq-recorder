# Contexto do projeto

GRS IQ Recorder: captura, replay e índice do fluxo de IQ da estação terrestre
SpaceLab. Único bloco da metade de RF escrito pela equipe — os outros três são
adotados do `spacelab-ufsc`.

**Estado: grava, reproduz e lê.** Épicos A, C e D fechados.

## Por que ele existe

Gravando o IQ **antes** do demodulador, a passagem vira artefato e o artefato
vira teste. Sem isso, validar a configuração do demodulador para o FS-2
dependeria de passagem ao vivo — e uma passagem de LEO dura dez minutos e não
se repete quando você quer.

## Desenho

Hexagonal, como o GRS Manager e o Station Manager.

```
domain/     models.py    value objects (CaptureProfile, IqBlock, CaptureMetadata)
            ports.py     Protocols (IqStreamSource, IqSink, IqStreamPublisher,
                         CaptureIndex, SpectrumView)
            capture.py   O CONTRATO (A4): SigMF, versões, sidecar. Função pura.
            profiles.py  Os perfis registrados (A3). Hoje: grs-rx-fs2.
adapters/   zmq_iq_source.py       C1  tap ao vivo no PUB :5556
            file_iq_sink.py        C2  grava o par SigMF + sha512
            file_iq_source.py      C3  lê uma captura, confere o hash
            zmq_iq_publisher.py    C3  republica no mesmo tópico
            postgres_capture_index.py  C4  índice append-only
            numpy_psd_view.py      D1  PSD, resumo, "tem sinal aqui?"
            wav_iq_source.py           IqStreamSource sobre um WAV do gqrx
application/ record.py  gravar: Source -> Sink -> Index
            replay.py  reproduzir: Source -> Publisher
schema_check.py  confere o índice no boot
config.py   o que vem do ambiente
main.py     boot
```

**A chave do desenho:** `ZmqIqSource` (tap ao vivo) e `FileIqSource` (replay)
implementam a **mesma porta**. Gravar de um rádio e reproduzir de um arquivo
são o mesmo laço com adapters diferentes.

## Decisões, e por quê

**O `.sigmf-data` é byte a byte o que veio do socket.** Sem normalização, sem
reordenamento, sem cabeçalho. Não é purismo: se o gravador transformasse as
amostras na entrada, o replay republicaria algo que o rádio nunca publicou, e
o demodulador estaria sendo exercitado contra um sinal inventado. A regressão
que isso esconde é a pior — a que só aparece com hardware real, depois de o
teste offline passar em verde.

**SigMF, e não um formato nosso.** Um `.iq` cru mais um `.json` inventado aqui
daria o mesmo trabalho e serviria só a nós. Com SigMF, a captura abre no
`inspectrum` sem conversão, e — o que interessa ao E2 — uma captura de
terceiros (uma passagem do FS-1 no SatNOGS) entra no harness de regressão sem
adaptador.

**Duas versões, independentes.** `SIGMF_VERSION` versiona o formato, que é de
terceiros; `CAPTURE_CONTRACT_VERSION` versiona o que a estação promete
preencher. O SigMF pode ficar parado anos enquanto a nossa convenção muda, e o
índice precisa saber qual das duas mudou para reler uma captura antiga.

**SHA-512, e não SHA-256.** Porque o campo do SigMF é `core:sha512`. Guardar
sha256 no índice daria duas somas para a mesma captura, e a do índice não
bateria com a do sidecar — que é justamente a conferência que interessa.

**Perfil é dado; ambiente é configuração.** Frequência e taxa NÃO são
variáveis de ambiente. Se fossem, a captura passaria a depender de qual era o
`.env` naquele dia, e deixaria de ser auto-descritiva.

**O serviço degrada sem Postgres.** Sem `PG_DATABASE_URL` ele grava igual, só
não indexa: a captura é o artefato, o índice é conveniência. Mesmo argumento
do painel do GRS Manager, que degrada sem o TC Scheduler em vez de falhar.

**O índice é append-only.** Uma captura é uma observação: aconteceu, num
instante, com uma configuração. Reescrever a linha depois é reescrever o que a
estação viu — e é assim que uma regressão de DSP fica impossível de reproduzir.

## Armadilhas conhecidas

- **`import-wav` produz RECONSTRUÇÃO, não captura de antena.** O WAV do gqrx
  em Narrow FM já passou pelo discriminador; o adapter integra aquilo de volta
  à fase. O resultado tem envoltória constante por construção, e o que o gqrx
  fez ao sinal (filtro, AGC, squelch) está embutido. Nada que dependa de
  amplitude está sendo exercitado.
- **A frequência de um `import-wav` é DECLARADA.** Áudio não carrega
  portadora: o número vem do que o operador diz ter sintonizado, e o sidecar o
  registra como `fixed`.

- **`peak_above_floor_db` usa a MEDIANA como piso, não a média.** A média é
  puxada para cima pelo próprio pico, e num sinal forte o piso pareceria mais
  alto do que é — a captura boa seria reportada como fraca.
- **A média de segmentos do PSD não deixa o pico mais ALTO; deixa-o no lugar
  CERTO.** Num único FFT um espinho aleatório de ruído vence um tom fraco de
  verdade. Depois de promediar, o piso fica liso e o tom, ainda que modesto,
  passa a ser o maior. Otimizar pela altura levaria à conclusão oposta.

- **No replay o publicador BINDA a :5556.** O `grs-iq-rx` e o `grs-sdr-sim`
  fazem o mesmo. Subir dois deles é disputa de porta, e quem perde cai em
  silêncio — não com erro.
- **`--fast` no replay estoura o demodulador.** Sem ritmo, a marca d'água de
  recepção enche e o ZMQ começa a DESCARTAR blocos. A perda aparece como falha
  de demodulação, que é o sintoma mais caro de diagnosticar. Use só com
  consumidor offline.
- **`CREATE TABLE IF NOT EXISTS` não adiciona coluna** a uma tabela que já
  existe. Uma versão nova contra um banco antigo escreve numa coluna que não
  está lá, e o erro aparece no FIM da gravação — quando a passagem já passou. É
  o que o `schema_check` pega no boot. Ver `docs/schema-contract.md`.
- **O perfil de um replay vem do ARQUIVO, não do registro.** Uma captura de
  seis meses atrás foi feita com a configuração daquele dia; relê-la com o
  perfil de hoje reescreveria a história.
- **Uma captura nunca é sobrescrita.** `FileIqSink` recusa um id que já existe:
  observação perdida não se refaz.

- **A frequência do `grs-rx-fs2` é PROVISÓRIA.** 145.9 MHz é a beacon do
  **FS-1**, e está lá pelo mesmo motivo que as coordenadas `GS_*` da estação
  são São Paulo: para o cano subir enquanto o número real não chega. A
  modulação do FS-2 está confirmada (2GFSK, syncword `BA 67 54 7E`); a
  frequência e o baud dependem da coordenação IARU. Trocar **antes** de
  qualquer campanha — captura na frequência errada é ruído gravado com muito
  capricho.
- **`grs:frequency_source: fixed` quer dizer frequência DECLARADA, não
  observada.** Ninguém conferiu no rádio; é o que o perfil mandou sintonizar.
  Numa captura que não demodula, "a frequência estava errada" continua sendo
  hipótese viva. Só vira observada com `tune_topic`, que depende do Doppler
  (fora desta fatia).
- **Ganho automático é AUSÊNCIA do campo `grs:gain_db`, não zero.** Escrever
  `0` afirmaria "0 dB", que é falso: com o AGC ligado ninguém sabe qual ganho
  o tuner escolheu.
- **O RTL-SDR só aceita 225001–300000 e 900001–3200000 S/s.** Pedir 48 kS/s
  (que é a constante `DEMOD_DEFAULT_SAMPLE_RATE` do `grs-demodulator`) **não
  dá erro** — o driver entrega outra taxa, calado, e a captura sai com o
  `core:sample_rate` errado no sidecar. Por isso o perfil usa 240 kS/s.
- **No replay o gravador BINDA a :5556** e o `grs-iq-rx` tem de estar
  **desligado** — os dois disputariam a mesma porta. Subir os dois juntos dá
  erro de bind, ou pior: o replay sobe primeiro e o `grs-iq-rx` cai em
  silêncio.
- **Captura é grande.** 240 kS/s x 8 B = 1,92 MB/s, ~115 MB por minuto. O
  `.gitignore` e o `.dockerignore` barram `*.sigmf-data` de propósito: sem
  isso, um `docker build` depois de uma sessão de gravação manda gigabytes ao
  daemon como build context.

## Convenções

- Comentários e mensagens de commit em português; código e identificadores em
  inglês. Como nos outros blocos da estação.
- Testes acompanham comportamento, não implementação.
- `docs/capture-contract.md` é **normativo**. Ele, `domain/capture.py` e
  `tests/test_iq_recorder_capture.py` mudam juntos — mexer num sem os outros
  é como o formato passa a divergir em silêncio.
