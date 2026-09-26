"""
Aster 0.6.6c - Context window fix: test di validazione di config.py.

Copre in particolare il nuovo campo opzionale ollama.num_ctx, introdotto
per correggere la regressione di routing memoria causata dal context
window implicito di Ollama (4096, mai configurato esplicitamente).

Aster 0.7.1b - Layered Configuration: default app-owned
(config.default.json) più override utente opzionale (DATA_ROOT/
config.json) limitato a una whitelist di 5 foglie, JSON stretto,
blocco B+ della config legacy, lettura unica in aster.main.

Nessun file in data/ viene mai letto o scritto: ogni test costruisce i
propri file di configurazione in una directory tempfile. L'unico file
reale letto (in sola lettura) è config.default.json.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import builtins
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aster
import modules.config as modulo_config
from modules.config import (
    FOGLIE_UTENTE,
    ErroreConfig,
    _unisci_override,
    carica_config,
    carica_config_runtime,
)
from modules.memory import MODALITA_NORMALE, StatoMemoria
from modules.runtime_paths import MODALITA_PORTABLE, PercorsiRuntime


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


# =====================================================================
# 0.7.1b - Layered Configuration
# =====================================================================

DEFAULT_REALE = BASE_DIR / "config.default.json"

# Configurazione runtime effettiva della baseline 0.7.1a (config.json
# v0.6.8 senza files e chat.stream): da sorgente, senza config utente,
# la configurazione finale deve restare identica. assistant.version
# resta 0.6.8 fino al release freeze della serie 0.7.
BASELINE_071A = {
    "assistant": {"name": "Aster", "version": "0.6.8"},
    "ollama": {
        "model": "qwen3:8b",
        "host": "http://localhost:11434",
        "timeout": 60,
        "num_ctx": 8192,
    },
    "chat": {"history_limit": 20},
    "memory": {"search_max_results": 5},
    "tools": {"filesystem": {"allowed_roots": []}},
}

# Sentinella: non deve mai comparire in un messaggio di errore.
SEGRETO = "SEGRETO-7f3a"


def _default_test() -> dict:
    """Default completo e valido per i test su tempfile."""

    return copy.deepcopy(BASELINE_071A)


def _senza_foglia(config: dict, percorso: tuple) -> dict:
    nodo = config
    for chiave in percorso[:-1]:
        nodo = nodo[chiave]
    del nodo[percorso[-1]]
    return config


def _istantanea(radice: Path) -> dict:
    """Ogni voce sotto radice: tipo, byte e mtime per i file."""

    risultato = {}
    for voce in sorted(radice.rglob("*")):
        relativo = str(voce.relative_to(radice))
        if voce.is_file():
            risultato[relativo] = ("file", voce.read_bytes(), voce.stat().st_mtime_ns)
        else:
            risultato[relativo] = ("dir", None, None)
    return risultato


@contextlib.contextmanager
def _vieta_scritture():
    """Qualsiasi primitiva di scrittura o creazione fa fallire il test."""

    open_originale = builtins.open
    os_open_originale = os.open
    flag_scrittura = (os.O_WRONLY | os.O_RDWR | os.O_CREAT
                      | os.O_APPEND | os.O_TRUNC)

    def open_sola_lettura(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError("scrittura non consentita")
        return open_originale(file, mode, *args, **kwargs)

    def os_open_sola_lettura(path, flags, *args, **kwargs):
        if flags & flag_scrittura:
            raise AssertionError("scrittura non consentita")
        return os_open_originale(path, flags, *args, **kwargs)

    def vietato(*args, **kwargs):
        raise AssertionError("scrittura non consentita")

    patch_attive = [
        mock.patch.object(builtins, "open", open_sola_lettura),
        mock.patch.object(io, "open", open_sola_lettura),
        mock.patch.object(os, "open", os_open_sola_lettura),
        *(mock.patch.object(os, nome, vietato)
          for nome in ("mkdir", "makedirs", "replace", "rename", "remove",
                       "unlink", "rmdir")),
        *(mock.patch.object(shutil, nome, vietato)
          for nome in ("copy", "copy2", "copyfile", "move", "rmtree")),
        *(mock.patch.object(Path, nome, vietato)
          for nome in ("write_text", "write_bytes", "touch", "mkdir",
                       "rename", "replace", "unlink", "rmdir")),
    ]
    with contextlib.ExitStack() as pila:
        for patch in patch_attive:
            pila.enter_context(patch)
        yield


class _BaseLivelli(unittest.TestCase):
    """Installazione simulata in tempfile: risorse, dati e app separati."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.risorse = self.tmp / "risorse"
        self.dati = self.tmp / "dati"
        self.app = self.tmp / "app"
        for directory in (self.risorse, self.dati, self.app):
            directory.mkdir()

        self.default_file = self.risorse / "config.default.json"
        self.user_file = self.dati / "config.json"
        self.legacy_file = self.app / "config.json"
        self._scrivi_json(self.default_file, _default_test())

    def tearDown(self):
        self._tmp.cleanup()

    def _scrivi_json(self, percorso: Path, contenuto, encoding="utf-8"):
        percorso.write_text(json.dumps(contenuto), encoding=encoding)

    def _carica(self) -> dict:
        return carica_config_runtime(
            self.default_file,
            self.user_file,
            self.legacy_file,
        )

    def _utente(self, contenuto) -> dict:
        self._scrivi_json(self.user_file, contenuto)
        return self._carica()

    def _senza_percorsi(self, messaggio: str) -> str:
        # I path tempfile possono contenere cifre casuali: si tolgono
        # prima di cercare valori numerici nel messaggio.
        return messaggio.replace(str(self.tmp), "<tmp>")

    def _assert_rifiutato(self, contenuto=None, testo=None, vietati=(),
                          tipo=ErroreConfig) -> str:
        if testo is not None:
            self.user_file.write_text(testo, encoding="utf-8")
        else:
            self._scrivi_json(self.user_file, contenuto)

        with self.assertRaises(tipo) as contesto:
            self._carica()

        self.assertIsInstance(contesto.exception, ErroreConfig)
        self.assertIsNone(contesto.exception.__cause__)
        messaggio = self._senza_percorsi(str(contesto.exception))
        for valore in vietati:
            self.assertNotIn(valore, messaggio)
        return messaggio


# ---------------------------------------------------------------------
# DEFAULT reale
# ---------------------------------------------------------------------


class TestDefaultReale(unittest.TestCase):
    """config.default.json del repository: letto soltanto."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Config utente e legacy inesistenti, in tempfile: mai data/ reale.
        self.user_file = self.tmp / "dati" / "config.json"
        self.legacy_file = self.tmp / "app" / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _grezzo(self) -> dict:
        return json.loads(DEFAULT_REALE.read_text(encoding="utf-8"))

    def _finale(self) -> dict:
        return carica_config_runtime(DEFAULT_REALE, self.user_file, self.legacy_file)

    def test_default_reale_valido_senza_utente_finale_uguale_default(self):
        prima = DEFAULT_REALE.read_bytes()

        self.assertEqual(self._finale(), self._grezzo())

        self.assertEqual(DEFAULT_REALE.read_bytes(), prima)
        self.assertFalse(self.user_file.parent.exists())
        self.assertFalse(self.legacy_file.parent.exists())

    def test_runtime_identico_alla_baseline_071a(self):
        self.assertEqual(self._finale(), BASELINE_071A)

    def test_allowed_roots_vuota(self):
        self.assertEqual(self._finale()["tools"]["filesystem"]["allowed_roots"], [])

    def test_files_assente(self):
        self.assertNotIn("files", self._grezzo())

    def test_chat_stream_assente(self):
        self.assertNotIn("stream", self._grezzo()["chat"])

    def test_foglie_whitelist_presenti(self):
        default = self._grezzo()
        for percorso in FOGLIE_UTENTE:
            with self.subTest(foglia=".".join(percorso)):
                nodo = default
                for chiave in percorso:
                    self.assertIn(chiave, nodo)
                    nodo = nodo[chiave]

    def test_whitelist_esattamente_cinque_foglie(self):
        self.assertEqual(len(FOGLIE_UTENTE), 5)
        self.assertEqual(set(FOGLIE_UTENTE), {
            ("ollama", "model"),
            ("ollama", "host"),
            ("ollama", "timeout"),
            ("ollama", "num_ctx"),
            ("tools", "filesystem", "allowed_roots"),
        })

    def test_default_utf8_senza_bom(self):
        self.assertFalse(DEFAULT_REALE.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_carica_config_compatibile_sul_default(self):
        self.assertEqual(carica_config(DEFAULT_REALE), self._grezzo())


# ---------------------------------------------------------------------
# OVERRIDE utente validi
# ---------------------------------------------------------------------


class TestOverrideUtente(_BaseLivelli):

    def test_nessun_file_utente_finale_uguale_default(self):
        shutil.rmtree(self.dati)

        self.assertEqual(self._carica(), _default_test())
        # Nessuna creazione: né DATA_ROOT né config utente.
        self.assertFalse(self.dati.exists())

    def test_override_model(self):
        finale = self._utente({"ollama": {"model": "llama3.1:8b"}})
        self.assertEqual(finale["ollama"]["model"], "llama3.1:8b")

    def test_override_host(self):
        finale = self._utente({"ollama": {"host": "http://192.168.1.20:11434"}})
        self.assertEqual(finale["ollama"]["host"], "http://192.168.1.20:11434")

    def test_override_timeout(self):
        for valore in (120, 12.5):
            with self.subTest(valore=valore):
                finale = self._utente({"ollama": {"timeout": valore}})
                self.assertEqual(finale["ollama"]["timeout"], valore)

    def test_override_num_ctx(self):
        # Uguale al default o superiore.
        for valore in (8192, 16384):
            with self.subTest(valore=valore):
                finale = self._utente({"ollama": {"num_ctx": valore}})
                self.assertEqual(finale["ollama"]["num_ctx"], valore)

    def test_override_allowed_roots(self):
        radici = ["./workspace", str(self.tmp / "documenti")]
        finale = self._utente({"tools": {"filesystem": {"allowed_roots": radici}}})
        self.assertEqual(finale["tools"]["filesystem"]["allowed_roots"], radici)

    def test_root_punto_ammessa(self):
        finale = self._utente({"tools": {"filesystem": {"allowed_roots": ["."]}}})
        self.assertEqual(finale["tools"]["filesystem"]["allowed_roots"], ["."])

    def test_piu_foglie_contemporanee(self):
        utente = {
            "ollama": {
                "model": "altro:14b",
                "host": "http://127.0.0.1:11500",
                "timeout": 30,
                "num_ctx": 32768,
            },
            "tools": {"filesystem": {"allowed_roots": ["."]}},
        }
        atteso = _default_test()
        atteso["ollama"] = dict(utente["ollama"])
        atteso["tools"]["filesystem"]["allowed_roots"] = ["."]

        self.assertEqual(self._utente(utente), atteso)

    def test_nested_parziale(self):
        atteso = _default_test()
        atteso["ollama"]["timeout"] = 90

        self.assertEqual(self._utente({"ollama": {"timeout": 90}}), atteso)

    def test_lista_sostituita_non_concatenata(self):
        default = _default_test()
        default["tools"]["filesystem"]["allowed_roots"] = ["./da-default"]
        self._scrivi_json(self.default_file, default)

        for lista in (["./da-utente"], [], ["./da-default", "./altra"]):
            with self.subTest(lista=lista):
                finale = self._utente({"tools": {"filesystem": {"allowed_roots": lista}}})
                self.assertEqual(finale["tools"]["filesystem"]["allowed_roots"], lista)

    def test_default_originale_non_mutato(self):
        default = _default_test()
        default["tools"]["filesystem"]["allowed_roots"] = ["./da-default"]
        istantanea = copy.deepcopy(default)

        finale = _unisci_override(
            default,
            {
                "ollama": {"model": "altro"},
                "tools": {"filesystem": {"allowed_roots": ["./nuova"]}},
            },
            "config utente di test",
        )

        self.assertEqual(default, istantanea)
        self.assertEqual(finale["ollama"]["model"], "altro")
        self.assertIsNot(finale["ollama"], default["ollama"])
        self.assertIsNot(finale["tools"]["filesystem"]["allowed_roots"],
                         default["tools"]["filesystem"]["allowed_roots"])

    def test_file_default_non_modificato(self):
        prima = self.default_file.read_bytes()
        self._utente({"ollama": {"model": "altro"}})
        self.assertEqual(self.default_file.read_bytes(), prima)

    def test_oggetti_vuoti_validi(self):
        for utente in (
            {},
            {"ollama": {}},
            {"tools": {}},
            {"tools": {"filesystem": {}}},
            {"ollama": {}, "tools": {"filesystem": {}}},
        ):
            with self.subTest(utente=utente):
                self.assertEqual(self._utente(utente), _default_test())

    def test_bom_utente_valido(self):
        self._scrivi_json(self.user_file, {"ollama": {"model": "con-bom"}},
                          encoding="utf-8-sig")
        self.assertTrue(self.user_file.read_bytes().startswith(b"\xef\xbb\xbf"))

        self.assertEqual(self._carica()["ollama"]["model"], "con-bom")

    def test_nessuna_scrittura(self):
        self._scrivi_json(self.user_file, {
            "ollama": {"num_ctx": 16384},
            "tools": {"filesystem": {"allowed_roots": ["./workspace"]}},
        })
        prima = _istantanea(self.tmp)

        with _vieta_scritture():
            self._carica()

        self.assertEqual(_istantanea(self.tmp), prima)


# ---------------------------------------------------------------------
# CONFIG utente non valida
# ---------------------------------------------------------------------


class TestConfigUtenteNonValida(_BaseLivelli):

    def test_chiave_root_sconosciuta(self):
        messaggio = self._assert_rifiutato({"sconosciuta": 1}, vietati=("sconosciuta",))
        # L'errore elenca le sole chiavi modificabili.
        for percorso in FOGLIE_UTENTE:
            self.assertIn(".".join(percorso), messaggio)

    def test_chiave_nested_sconosciuta(self):
        for utente in (
            {"ollama": {"temperature": 0.2}},
            {"tools": {"shell": {}}},
            {"tools": {"filesystem": {"denied_roots": []}}},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, vietati=("temperature", "shell", "denied_roots"))

    def test_typo_num_ctz(self):
        messaggio = self._assert_rifiutato({"ollama": {"num_ctz": 16384}},
                                           vietati=("num_ctz", "16384"))
        self.assertIn("'ollama'", messaggio)

    def test_assistant_rifiutato(self):
        for utente in (
            {"assistant": {"name": "Jarvis"}},
            {"assistant": {"version": "9.9"}},
            {"assistant": {}},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, vietati=("Jarvis", "9.9"))

    def test_files_rifiutato(self):
        for utente in (
            {"files": {"memory": "altrove/memory.json"}},
            {"files": {"prompt": "prompt.txt"}},
            {"files": {}},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, vietati=("altrove",))

    def test_chat_rifiutato(self):
        for utente in (
            {"chat": {"history_limit": 200}},
            {"chat": {"stream": False}},
            {"chat": {}},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, vietati=("200",))

    def test_memory_rifiutato(self):
        for utente in ({"memory": {"search_max_results": 50}}, {"memory": {}}):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente)

    def test_version_e_path_runtime_rifiutati(self):
        for utente in (
            {"version": "1.0"},
            {"data_root": "D:\\AsterDati"},
            {"app_root": "."},
            {"resource_root": "."},
            {"prompt": "altro.txt"},
            {"memory_file": "altro.json"},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, vietati=("AsterDati", "altro"))

    def test_tipo_errato(self):
        for utente in (
            {"ollama": {"model": 42}},
            {"ollama": {"model": ["qwen3:8b"]}},
            {"ollama": {"host": None}},
            {"ollama": {"timeout": "60"}},
            {"ollama": {"num_ctx": "16384"}},
            {"ollama": {"num_ctx": 16384.0}},
            {"ollama": "qwen3:8b"},
            {"ollama": None},
            {"tools": []},
            {"tools": {"filesystem": ["."]}},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, tipo=TypeError)

    def test_json_non_oggetto(self):
        for testo in ("[]", '"testo"', "42", "null", "true"):
            with self.subTest(testo=testo):
                self._assert_rifiutato(testo=testo, tipo=TypeError)

    def test_bool_al_posto_di_numero(self):
        for utente in (
            {"ollama": {"timeout": True}},
            {"ollama": {"timeout": False}},
            {"ollama": {"num_ctx": True}},
            {"ollama": {"num_ctx": False}},
        ):
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, tipo=TypeError)

    def test_timeout_non_positivo(self):
        for valore in (0, 0.0, -1, -0.5):
            with self.subTest(valore=valore):
                self._assert_rifiutato({"ollama": {"timeout": valore}})

    def test_timeout_nan_o_infinito(self):
        for letterale in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(letterale=letterale):
                self._assert_rifiutato(testo='{"ollama": {"timeout": %s}}' % letterale)

    def test_num_ctx_sotto_default(self):
        for valore in (4096, 8191):
            with self.subTest(valore=valore):
                messaggio = self._assert_rifiutato({"ollama": {"num_ctx": valore}},
                                                   vietati=(str(valore), "8192"))
                self.assertIn("ollama.num_ctx", messaggio)

    def test_num_ctx_non_positivo(self):
        for valore in (0, -8192):
            with self.subTest(valore=valore):
                self._assert_rifiutato({"ollama": {"num_ctx": valore}})

    def test_model_host_vuoti_o_con_caratteri_di_controllo(self):
        for campo in ("model", "host"):
            for valore in ("", "qwen3\n8b", "http://h\x00", "a\x7fb", "\tqwen3"):
                with self.subTest(campo=campo, valore=valore):
                    self._assert_rifiutato({"ollama": {campo: valore}})

    def test_allowed_roots_non_lista(self):
        for valore in ("C:\\dati", {"root": "."}, None, 1):
            with self.subTest(valore=valore):
                self._assert_rifiutato(
                    {"tools": {"filesystem": {"allowed_roots": valore}}},
                    tipo=TypeError,
                )

    def test_allowed_roots_elemento_non_stringa(self):
        for valore in ([1], [None], [["."]], [True], [".", 2]):
            with self.subTest(valore=valore):
                self._assert_rifiutato(
                    {"tools": {"filesystem": {"allowed_roots": valore}}},
                    tipo=TypeError,
                )

    def test_allowed_roots_elemento_vuoto(self):
        for valore in ([""], [".", ""]):
            with self.subTest(valore=valore):
                self._assert_rifiutato({"tools": {"filesystem": {"allowed_roots": valore}}})

    def test_allowed_roots_elemento_con_nul(self):
        self._assert_rifiutato(
            {"tools": {"filesystem": {"allowed_roots": ["dati\x00altro"]}}}
        )

    def test_chiavi_duplicate(self):
        for testo in (
            '{"ollama": {"model": "a", "model": "b"}}',
            '{"ollama": {}, "ollama": {}}',
            '{"tools": {"filesystem": {"allowed_roots": [], "allowed_roots": ["."]}}}',
        ):
            with self.subTest(testo=testo):
                self._assert_rifiutato(testo=testo)

    def test_json_malformato(self):
        for testo in (
            "",
            "{",
            '{"ollama": {"model": "x",}}',
            "non json",
            "{'ollama': {}}",
            '{"ollama": {}} extra',
        ):
            with self.subTest(testo=testo):
                self._assert_rifiutato(testo=testo)

    def test_config_utente_directory(self):
        self.user_file.mkdir()
        with self.assertRaises(ErroreConfig):
            self._carica()

    def test_config_utente_non_utf8(self):
        self.user_file.write_bytes(b'{"ollama": {"model": "\xff\xfe"}}')
        with self.assertRaises(ErroreConfig):
            self._carica()

    def test_errore_os_in_lettura(self):
        self._scrivi_json(self.user_file, {})
        open_originale = builtins.open

        def open_negato(file, *args, **kwargs):
            if Path(file) == self.user_file:
                raise PermissionError(13, "Accesso negato", str(self.tmp / SEGRETO))
            return open_originale(file, *args, **kwargs)

        with mock.patch.object(builtins, "open", open_negato):
            with self.assertRaises(ErroreConfig) as contesto:
                self._carica()

        self.assertNotIn(SEGRETO, str(contesto.exception))
        self.assertIsNone(contesto.exception.__cause__)

    def test_errore_os_in_verifica_esistenza(self):
        lstat_originale = os.lstat

        def lstat_negato(percorso, *args, **kwargs):
            if Path(percorso) == self.user_file:
                raise PermissionError(13, "Accesso negato", SEGRETO)
            return lstat_originale(percorso, *args, **kwargs)

        with mock.patch.object(os, "lstat", lstat_negato):
            with self.assertRaises(ErroreConfig) as contesto:
                self._carica()

        self.assertNotIn(SEGRETO, str(contesto.exception))

    def test_messaggi_senza_valori_sensibili(self):
        casi_json = (
            {SEGRETO: 1},
            {"ollama": {SEGRETO: 1}},
            {"tools": {"filesystem": {SEGRETO: []}}},
            {"assistant": {"name": SEGRETO}},
            {"ollama": {"model": SEGRETO + "\n"}},
            {"ollama": {"host": SEGRETO + "\x07"}},
            {"ollama": {"timeout": SEGRETO}},
            {"ollama": {"num_ctx": SEGRETO}},
            {"tools": {"filesystem": {"allowed_roots": SEGRETO}}},
            {"tools": {"filesystem": {"allowed_roots": [SEGRETO + "\x00"]}}},
            {"tools": {"filesystem": {"allowed_roots": [SEGRETO, ""]}}},
            {"tools": {"filesystem": {"allowed_roots": [SEGRETO, 1]}}},
        )
        casi_testo = (
            '{"%s": 1, "%s": 2}' % (SEGRETO, SEGRETO),
            '{"ollama": {"model": "%s"' % SEGRETO,
            '{"ollama": {"model": "%s", "timeout": NaN}}' % SEGRETO,
            SEGRETO,
        )

        for utente in casi_json:
            with self.subTest(utente=utente):
                self._assert_rifiutato(utente, vietati=(SEGRETO,))
        for testo in casi_testo:
            with self.subTest(testo=testo):
                self._assert_rifiutato(testo=testo, vietati=(SEGRETO,))


# ---------------------------------------------------------------------
# DEFAULT non valido: avvio bloccato
# ---------------------------------------------------------------------


class TestDefaultNonValido(_BaseLivelli):

    def _assert_default_rifiutato(self, default=None, testo=None) -> str:
        if testo is not None:
            self.default_file.write_text(testo, encoding="utf-8")
        elif default is not None:
            self._scrivi_json(self.default_file, default)

        with self.assertRaises(ErroreConfig) as contesto:
            self._carica()
        return str(contesto.exception)

    def test_default_mancante(self):
        self.default_file.unlink()
        self._assert_default_rifiutato()

    def test_default_directory(self):
        self.default_file.unlink()
        self.default_file.mkdir()
        self._assert_default_rifiutato()

    def test_foglia_whitelist_mancante(self):
        for percorso in FOGLIE_UTENTE:
            with self.subTest(foglia=".".join(percorso)):
                messaggio = self._assert_default_rifiutato(
                    _senza_foglia(_default_test(), percorso)
                )
                self.assertIn(".".join(percorso), messaggio)

    def test_foglia_app_owned_mancante(self):
        for percorso in (
            ("assistant", "name"),
            ("assistant", "version"),
            ("chat", "history_limit"),
            ("memory", "search_max_results"),
            ("ollama",),
            ("tools",),
        ):
            with self.subTest(foglia=".".join(percorso)):
                self._assert_default_rifiutato(_senza_foglia(_default_test(), percorso))

    def test_default_con_valori_non_validi(self):
        modifiche = (
            (("tools", "filesystem", "allowed_roots"), [""]),
            (("tools", "filesystem"), "."),
            (("ollama", "num_ctx"), "8192"),
            (("ollama", "timeout"), 0),
            (("ollama", "model"), ""),
            (("chat", "history_limit"), "20"),
            (("memory", "search_max_results"), 0),
            (("assistant", "name"), ""),
        )
        for percorso, valore in modifiche:
            with self.subTest(campo=".".join(percorso)):
                default = _default_test()
                nodo = default
                for chiave in percorso[:-1]:
                    nodo = nodo[chiave]
                nodo[percorso[-1]] = valore
                self._assert_default_rifiutato(default)

    def test_default_json_stretto(self):
        for testo in (
            '{"assistant": {}, "assistant": {}}',
            json.dumps(_default_test()).replace('"timeout": 60', '"timeout": NaN'),
            json.dumps(_default_test()).replace('"timeout": 60', '"timeout": Infinity'),
        ):
            with self.subTest(testo=testo[:40]):
                self._assert_default_rifiutato(testo=testo)

    def test_default_con_bom_rifiutato(self):
        # Il default app-owned si legge in UTF-8 stretto; il BOM è
        # tollerato solo nel config utente.
        self._scrivi_json(self.default_file, _default_test(), encoding="utf-8-sig")
        self._assert_default_rifiutato()


# ---------------------------------------------------------------------
# chat.history_limit: app-owned, intero reale >= 1
# ---------------------------------------------------------------------


class TestHistoryLimit(_BaseLivelli):

    def _default_con_history_limit(self, valore):
        default = _default_test()
        default["chat"]["history_limit"] = valore
        self._scrivi_json(self.default_file, default)

    def test_default_valido(self):
        for valore in (1, 20):
            with self.subTest(valore=valore):
                self._default_con_history_limit(valore)

                finale = self._carica()

                self.assertIs(type(finale["chat"]["history_limit"]), int)
                self.assertEqual(finale["chat"]["history_limit"], valore)

    def test_default_bool_rifiutato(self):
        for valore in (True, False):
            with self.subTest(valore=valore):
                self._default_con_history_limit(valore)

                with self.assertRaises(TypeError) as contesto:
                    self._carica()

                self.assertIsInstance(contesto.exception, ErroreConfig)
                self.assertIn("chat.history_limit", str(contesto.exception))

    def test_default_non_positivo_rifiutato(self):
        for valore in (0, -1):
            with self.subTest(valore=valore):
                self._default_con_history_limit(valore)

                with self.assertRaises(ErroreConfig) as contesto:
                    self._carica()

                messaggio = self._senza_percorsi(str(contesto.exception))
                self.assertIn("chat.history_limit", messaggio)
                self.assertNotIn(str(valore), messaggio)

    def test_non_sovrascrivibile_dal_config_utente(self):
        # Anche un valore valido resta app-owned: nessuna whitelist.
        self.assertNotIn(("chat", "history_limit"), FOGLIE_UTENTE)
        for valore in (1, 20):
            with self.subTest(valore=valore):
                self._assert_rifiutato({"chat": {"history_limit": valore}})


# ---------------------------------------------------------------------
# LEGACY B+
# ---------------------------------------------------------------------

CONFIG_LEGACY = {
    "assistant": {"name": "Aster", "version": "0.6.8"},
    "ollama": {
        "model": "modello-" + SEGRETO,
        "host": "http://" + SEGRETO + ":11434",
        "timeout": 60,
        "num_ctx": 16384,
    },
    "chat": {"history_limit": 20, "stream": True},
    "memory": {"search_max_results": 5},
    "files": {"prompt": "prompt.txt", "memory": "data/memory.json"},
    "tools": {"filesystem": {"allowed_roots": ["C:\\" + SEGRETO]}},
}


class TestLegacyBPlus(_BaseLivelli):

    def _scrivi_legacy(self) -> bytes:
        self.legacy_file.write_text(json.dumps(CONFIG_LEGACY, indent=4), encoding="utf-8")
        return self.legacy_file.read_bytes()

    def _carica_bloccato(self) -> str:
        with self.assertRaises(ErroreConfig) as contesto:
            self._carica()
        self.assertIsNone(contesto.exception.__cause__)
        return str(contesto.exception)

    def test_legacy_assente_ok(self):
        self.assertFalse(self.legacy_file.exists())
        self.assertEqual(self._carica(), _default_test())

    def test_legacy_presente_separato_blocca(self):
        self._scrivi_legacy()

        messaggio = self._carica_bloccato()

        self.assertIn("legacy", messaggio.lower())
        self.assertIn(str(self.legacy_file), messaggio)
        self.assertIn(str(self.user_file), messaggio)
        for percorso in FOGLIE_UTENTE:
            self.assertIn(".".join(percorso), messaggio)

    def test_legacy_messaggio_senza_valori(self):
        self._scrivi_legacy()

        messaggio = self._senza_percorsi(self._carica_bloccato())

        self.assertNotIn(SEGRETO, messaggio)
        self.assertNotIn("16384", messaggio)
        self.assertNotIn("qwen", messaggio)

    def test_legacy_blocca_anche_con_config_utente_valida(self):
        self._scrivi_legacy()
        self._scrivi_json(self.user_file, {"ollama": {"model": "utente"}})
        self._carica_bloccato()

    def test_legacy_blocca_in_qualsiasi_forma(self):
        # Anche una directory omonima: nessuna interpretazione del contenuto.
        self.legacy_file.mkdir()
        self._carica_bloccato()

    def test_legacy_non_letto_nulla_letto_prima_del_blocco(self):
        self._scrivi_legacy()
        aperti = []
        open_originale = builtins.open

        def open_spia(file, *args, **kwargs):
            aperti.append(file)
            return open_originale(file, *args, **kwargs)

        with mock.patch.object(builtins, "open", open_spia):
            self._carica_bloccato()

        self.assertEqual(aperti, [])

    def test_legacy_errore_senza_scritture(self):
        self._scrivi_legacy()
        prima = _istantanea(self.tmp)

        with _vieta_scritture():
            self._carica_bloccato()

        self.assertEqual(_istantanea(self.tmp), prima)

    def test_legacy_non_modificato_byte_per_byte(self):
        contenuto = self._scrivi_legacy()
        mtime = self.legacy_file.stat().st_mtime_ns

        self._carica_bloccato()

        self.assertEqual(self.legacy_file.read_bytes(), contenuto)
        self.assertEqual(self.legacy_file.stat().st_mtime_ns, mtime)
        # Nessun backup, rinomina o copia accanto al legacy.
        self.assertEqual([voce.name for voce in self.app.iterdir()], ["config.json"])

    def test_config_utente_e_data_root_non_creati(self):
        shutil.rmtree(self.dati)
        self._scrivi_legacy()

        self._carica_bloccato()

        self.assertFalse(self.dati.exists())
        self.assertFalse(self.user_file.exists())

    def test_legacy_uguale_utente_letto_una_sola_volta_come_utente(self):
        self._scrivi_json(self.user_file, {"ollama": {"model": "modello-utente"}})

        with mock.patch.object(modulo_config, "_leggi_json",
                               wraps=modulo_config._leggi_json) as spia:
            finale = carica_config_runtime(self.default_file, self.user_file, self.user_file)

        self.assertEqual(finale["ollama"]["model"], "modello-utente")
        self.assertEqual([chiamata.args[0] for chiamata in spia.call_args_list],
                         [self.default_file, self.user_file])

    def test_legacy_stesso_file_con_percorso_diverso(self):
        # Stesso file raggiunto da un percorso scritto diversamente.
        self._scrivi_json(self.user_file, {"ollama": {"model": "modello-utente"}})
        alias = self.app / ".." / "dati" / "config.json"

        finale = carica_config_runtime(self.default_file, self.user_file, alias)

        self.assertEqual(finale["ollama"]["model"], "modello-utente")


# ---------------------------------------------------------------------
# SINGLE LOAD: aster.main
# ---------------------------------------------------------------------


class TestAvvioAsterSingleLoad(_BaseLivelli):
    """aster.main su installazione tempfile: niente Ollama, niente data/ reale."""

    def setUp(self):
        super().setUp()
        (self.risorse / "prompt.txt").write_text("prompt di test", encoding="utf-8")
        self.radice_assoluta = self.tmp / "esterna"
        self.percorsi = PercorsiRuntime(
            modalita=MODALITA_PORTABLE,
            app_root=self.app,
            resource_root=self.risorse,
            data_root=self.dati,
        )
        self.assertEqual(self.percorsi.default_config_file, self.default_file)
        self.assertEqual(self.percorsi.user_config_file, self.user_file)
        self.assertEqual(self.percorsi.legacy_config_file, self.legacy_file)

        self.preparazioni = []
        self.avvii_chat = []
        self.controlli_ollama = []
        self.memorie = []

    def _esegui_main(self):
        prepara_reale = aster.prepara_contesto_filesystem

        def prepara_spia(config, base_dir):
            contesto = prepara_reale(config, base_dir)
            self.preparazioni.append((copy.deepcopy(config), base_dir, contesto))
            return contesto

        def memoria_finta(percorso):
            self.memorie.append(percorso)
            return StatoMemoria(memoria=None, modalita=MODALITA_NORMALE)

        uscita = io.StringIO()
        with mock.patch.object(aster, "percorsi_runtime", lambda: self.percorsi), \
                mock.patch.object(aster, "prepara_contesto_filesystem", prepara_spia), \
                mock.patch.object(aster, "inizializza_memoria", memoria_finta), \
                mock.patch.object(aster, "controlla_ollama",
                                  lambda *a: self.controlli_ollama.append(a)), \
                mock.patch.object(aster, "avvia_chat",
                                  lambda *a: self.avvii_chat.append(a)), \
                mock.patch.object(aster, "carica_config_runtime",
                                  wraps=aster.carica_config_runtime) as spia_config, \
                mock.patch.object(modulo_config, "_leggi_json",
                                  wraps=modulo_config._leggi_json) as spia_letture, \
                contextlib.redirect_stdout(uscita):
            aster.main()

        return spia_config, spia_letture, uscita.getvalue()

    def test_override_allowed_roots_contesto_passato_ad_avvia_chat(self):
        radici = ["./workspace", str(self.radice_assoluta)]
        self._scrivi_json(self.user_file, {
            "ollama": {"num_ctx": 16384},
            "tools": {"filesystem": {"allowed_roots": radici}},
        })

        spia_config, spia_letture, _ = self._esegui_main()

        # Lettura unica: default e config utente, una volta ciascuno.
        spia_config.assert_called_once_with(
            self.default_file, self.user_file, self.legacy_file
        )
        self.assertEqual([chiamata.args[0] for chiamata in spia_letture.call_args_list],
                         [self.default_file, self.user_file])

        # prepara_contesto_filesystem: config finale + APP_ROOT.
        self.assertEqual(len(self.preparazioni), 1)
        config_passato, base_dir, contesto = self.preparazioni[0]
        self.assertEqual(base_dir, self.app)
        self.assertEqual(config_passato["tools"]["filesystem"]["allowed_roots"], radici)
        # Relative rispetto ad app_root: mai data_root, resource_root o cwd.
        self.assertEqual(contesto.allowed_roots,
                         [self.app / "workspace", self.radice_assoluta])

        # avvia_chat riceve esattamente quel ContestoFilesystem.
        self.assertEqual(len(self.avvii_chat), 1)
        argomenti = self.avvii_chat[0]
        self.assertEqual(len(argomenti), 10)
        self.assertIs(argomenti[-1], contesto)
        self.assertEqual(argomenti[1], "qwen3:8b")
        self.assertEqual(argomenti[5], 16384)
        self.assertEqual(argomenti[7], self.percorsi.memory_file)
        self.assertEqual(self.memorie, [self.percorsi.memory_file])

    def test_senza_config_utente_default_deny(self):
        spia_config, _, _ = self._esegui_main()

        spia_config.assert_called_once()
        _, base_dir, contesto = self.preparazioni[0]
        self.assertEqual(base_dir, self.app)
        self.assertEqual(contesto.allowed_roots, [])
        self.assertIs(self.avvii_chat[0][-1], contesto)
        self.assertEqual(self.avvii_chat[0][5], 8192)
        self.assertFalse(self.user_file.exists())

    def test_legacy_blocca_avvio_prima_della_chat(self):
        self.legacy_file.write_text(json.dumps(CONFIG_LEGACY), encoding="utf-8")

        _, spia_letture, uscita = self._esegui_main()

        self.assertIn("Impossibile avviare Aster", uscita)
        self.assertNotIn(SEGRETO, uscita)
        self.assertEqual(spia_letture.call_args_list, [])
        self.assertEqual(self.preparazioni, [])
        self.assertEqual(self.memorie, [])
        self.assertEqual(self.controlli_ollama, [])
        self.assertEqual(self.avvii_chat, [])
        self.assertFalse(self.user_file.exists())

    def test_config_utente_non_valida_blocca_avvio(self):
        self._scrivi_json(self.user_file, {"chat": {"history_limit": 999}})

        _, _, uscita = self._esegui_main()

        self.assertIn("Impossibile avviare Aster", uscita)
        self.assertNotIn("999", uscita.replace(str(self.tmp), "<tmp>"))
        self.assertEqual(self.preparazioni, [])
        self.assertEqual(self.avvii_chat, [])


if __name__ == "__main__":
    unittest.main()
