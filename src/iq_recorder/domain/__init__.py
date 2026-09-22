"""Modelo, portas e contrato de captura do GRS IQ Recorder.

Nada aqui importa zmq, numpy, SQLAlchemy ou os blocos adotados de RF. A
fronteira é de propósito: é o que permite testar o contrato de captura e os
perfis sem subir socket nenhum, e é o que mantém o gravador independente dos
três repositórios de terceiros que a estação adotou.
"""
