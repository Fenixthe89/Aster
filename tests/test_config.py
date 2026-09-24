"""
Aster 0.6.6c - Context window fix: test di validazione di config.py.

Copre in particolare il nuovo campo opzionale ollama.num_ctx, introdotto
per correggere la regressione di routing memoria causata dal context
window implicito di Ollama (4096, mai configurato esplicitamente).

Nessun file reale (config.json/data/) viene mai letto o scritto: ogni
test costruisce un config.json temporaneo in una directory tempfile.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from modules.config import carica_config


def _config_base(ollama_extra: dict | None = None) -> dict:
    """
    Config minimo valido, con tutte le chiavi obbligatorie lette da
    carica_config. ollama_extra viene fuso dentro la sezione "ollama".
    """

    ollama = {
        "model": "qwen3:8b",
        "host": "http://localhost:11434",
        "timeout": 60,
    }

    if ollama_extra:
        ollama.update(ollama_extra)

    return {
        "assistant": {"name": "Aster", "version": "0.5.3"},
        "ollama": ollama,
        "chat": {"history_limit": 20, "stream": True},
        "memory": {"search_max_results": 5},
        "files": {"prompt": "prompt.txt", "memory": "data/memory.json"},
    }


def _scrivi_config_temporaneo(config: dict, directory: Path) -> Path:
    percorso = directory / "config.json"
    percorso.write_text(json.dumps(config), encoding="utf-8")
    return percorso


class TestValidazioneNumCtx(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_num_ctx_assente_non_solleva_errore(self):
        config = _config_base()
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        caricato = carica_config(percorso)

        self.assertNotIn("num_ctx", caricato["ollama"])

    def test_num_ctx_esplicito_valido_preservato(self):
        config = _config_base({"num_ctx": 4096})
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        caricato = carica_config(percorso)

        self.assertEqual(caricato["ollama"]["num_ctx"], 4096)

    def test_num_ctx_zero_solleva_value_error(self):
        config = _config_base({"num_ctx": 0})
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        with self.assertRaises(ValueError):
            carica_config(percorso)

    def test_num_ctx_negativo_solleva_value_error(self):
        config = _config_base({"num_ctx": -1})
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        with self.assertRaises(ValueError):
            carica_config(percorso)

    def test_num_ctx_stringa_solleva_type_error(self):
        config = _config_base({"num_ctx": "8192"})
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        with self.assertRaises(TypeError):
            carica_config(percorso)

    def test_num_ctx_bool_solleva_type_error(self):
        config = _config_base({"num_ctx": True})
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        with self.assertRaises(TypeError):
            carica_config(percorso)

    def test_num_ctx_grande_valido(self):
        config = _config_base({"num_ctx": 40960})
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        caricato = carica_config(percorso)

        self.assertEqual(caricato["ollama"]["num_ctx"], 40960)

    def test_altri_contratti_config_non_alterati(self):
        # timeout e allowed_roots devono continuare a comportarsi come
        # prima dell'introduzione di num_ctx.
        config = _config_base({"num_ctx": 8192})
        config["ollama"]["timeout"] = -5
        percorso = _scrivi_config_temporaneo(config, self.tmp_dir)

        with self.assertRaises(ValueError):
            carica_config(percorso)


if __name__ == "__main__":
    unittest.main()
