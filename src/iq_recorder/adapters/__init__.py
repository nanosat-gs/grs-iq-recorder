"""Adapters do GRS IQ Recorder: as implementações concretas das portas.

Vazio no A2, e povoado pelo Épico C:
    ZmqIqSource          (C1) IqStreamSource — tap no PUB :5556 do grs-iq-rx
    FileIqSink           (C2) IqSink — escreve o par SigMF e o hash
    FileIqSource         (C3) IqStreamSource — lê uma captura, para replay
    ZmqIqPublisher       (C3) IqStreamPublisher — republica no mesmo tópico
    PostgresCaptureIndex (C4) CaptureIndex — índice append-only
    NumpyPsdView         (D1) SpectrumView — PSD e resumo
"""
