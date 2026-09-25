"""
Aster 0.6.6c - Context window fix: test di modules/ollama_manager.py.

Verifica che ogni funzione che invoca client.chat(...) passi options
con la chiave "num_ctx" ESATTAMENTE come ricevuta (esplicita o di
default), senza sovrascrivere altre eventuali chiavi di options in
futuro (qui non ce ne sono ancora, ma la forma del dict viene
verificata puntualmente).

Nessuna vera chiamata di rete: modules.ollama_manager.crea_client
viene sempre sostituito con un doppio di test che restituisce un
client fittizio (spia) al posto di ollama.Client.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.ollama_manager as ollama_manager
from modules.ollama_manager import (
    avvia_stream,
    esegui_risposta_finale,
    esegui_turno_con_tools,
)


class ClientFittizio:
    """Spia: registra gli argomenti dell'ultima chiamata a chat()."""

    def __init__(self):
        self.ultima_chiamata = None

    def chat(self, **kwargs):
        self.ultima_chiamata = kwargs
        return "risposta fittizia"


def _crea_client_fittizio_factory(client):
    def _crea_client(host, timeout=60):
        return client

    return _crea_client


class TestNumCtxOllamaManager(unittest.TestCase):

    def setUp(self):
        self.client = ClientFittizio()
        self._originale_crea_client = ollama_manager.crea_client
        ollama_manager.crea_client = _crea_client_fittizio_factory(self.client)

    def tearDown(self):
        ollama_manager.crea_client = self._originale_crea_client

    # -----------------------------------------------------------
    # esegui_turno_con_tools
    # -----------------------------------------------------------

    def test_esegui_turno_con_tools_num_ctx_esplicito(self):
        esegui_turno_con_tools(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
            [],
            "http://localhost:11434",
            60,
            12345,
        )

        self.assertEqual(
            self.client.ultima_chiamata["options"],
            {"num_ctx": 12345},
        )

    def test_esegui_turno_con_tools_num_ctx_default(self):
        esegui_turno_con_tools(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
            [],
            "http://localhost:11434",
        )

        self.assertEqual(
            self.client.ultima_chiamata["options"],
            {"num_ctx": 8192},
        )

    def test_esegui_turno_con_tools_altri_parametri_invariati(self):
        esegui_turno_con_tools(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
            [{"type": "function"}],
            "http://localhost:11434",
            60,
            8192,
        )

        chiamata = self.client.ultima_chiamata
        self.assertEqual(chiamata["model"], "qwen3:8b")
        self.assertEqual(chiamata["tools"], [{"type": "function"}])
        self.assertFalse(chiamata["stream"])
        self.assertFalse(chiamata["think"])

    # -----------------------------------------------------------
    # esegui_risposta_finale
    # -----------------------------------------------------------

    def test_esegui_risposta_finale_num_ctx_esplicito(self):
        esegui_risposta_finale(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
            "http://localhost:11434",
            60,
            9999,
        )

        self.assertEqual(
            self.client.ultima_chiamata["options"],
            {"num_ctx": 9999},
        )

    def test_esegui_risposta_finale_num_ctx_default(self):
        esegui_risposta_finale(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
            "http://localhost:11434",
        )

        self.assertEqual(
            self.client.ultima_chiamata["options"],
            {"num_ctx": 8192},
        )

    # -----------------------------------------------------------
    # avvia_stream
    # -----------------------------------------------------------

    def test_avvia_stream_num_ctx_esplicito(self):
        avvia_stream(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
            "http://localhost:11434",
            60,
            16384,
        )

        self.assertEqual(
            self.client.ultima_chiamata["options"],
            {"num_ctx": 16384},
        )

    def test_avvia_stream_num_ctx_default(self):
        avvia_stream(
            "qwen3:8b",
            [{"role": "user", "content": "ciao"}],
        )

        self.assertEqual(
            self.client.ultima_chiamata["options"],
            {"num_ctx": 8192},
        )


if __name__ == "__main__":
    unittest.main()
