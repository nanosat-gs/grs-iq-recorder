"""Casos de uso do GRS IQ Recorder: os laços que ligam as portas.

Vazio no A2. Serão dois, e a simetria entre eles é o ponto do serviço:

    gravar    IqStreamSource -> IqSink -> CaptureIndex
    reproduzir  IqStreamSource -> IqStreamPublisher

O mesmo tipo de porta está na entrada dos dois. Gravar de um rádio e
reproduzir de um arquivo são o mesmo laço com adapters diferentes — e é por
isso que o teste ponta a ponta do E3 roda sem hardware nenhum.
"""
