"""GRS IQ Recorder — captura, replay e índice do fluxo de IQ da estação.

O único bloco da metade de RF escrito pela equipe. Os outros três
(`grs-iq-rx`, `grs-demodulator`, `grs-syncword-detector`) são adotados do
spacelab-ufsc; este nasce aqui porque não existe lá.

A razão de ele existir: gravando o IQ — banda-base complexa, ANTES do
demodulador — a passagem vira artefato, e o artefato vira teste. O demod e o
detector de syncword passam a ser exercitáveis offline, determinísticos, sem
SDR e sem esperar o satélite passar de novo.
"""
