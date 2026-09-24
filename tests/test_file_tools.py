"""
0.6.6a - list_directory: test di regressione.

Verifica il primo tool filesystem reale di Aster: integrazione config
(solo forma grezza in modules/config.py, semantica in file_tools.py),
default deny, l'handler reale di list_directory (containment via
filesystem_policy.py, invariato, doppia validazione TOCTOU), privacy
dell'output (nessun path in nessun risultato), classificazione symlink
senza seguirne il target, registrazione nel registry combinato,
fallback deterministico e pipeline post-tool generica.

Usa esclusivamente tempfile/scratch: nessun file in data/ né in
config.json reale viene mai letto o scritto. Nessuna chiamata a
Ollama: esegui_risposta_finale viene sempre sostituita con un doppio
di test.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.file_tools as file_tools
import modules.tool_response as tool_response
from modules.config import carica_config
from modules.file_tools import (
    MAX_DIRECTORY_ENTRIES,
    ContestoFilesystem,
    _prepara_allowed_roots,
    fallback_deterministico_file,
    list_directory,
    prepara_contesto_filesystem,
    registra_tool_filesystem,
)
from modules.filesystem_policy import PathDecision
from modules.tool_registry import crea_registro_memoria
from modules.tool_response import genera_risposta_post_tool


def _symlink_supportato(sorgente: Path, link: Path) -> bool:
    try:
        link.symlink_to(sorgente, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def _messaggio(contenuto: str):
    return SimpleNamespace(message=SimpleNamespace(content=contenuto))


class ContatoreChiamate:
    def __init__(self, comportamento):
        self.chiamate = 0
        self._comportamento = comportamento

    def __call__(self, modello, messaggi, host_ollama, timeout_ollama):
        self.chiamate += 1
        return self._comportamento()


class FileToolsTestCase(unittest.TestCase):
    """Base comune: directory temporanea isolata per ogni test."""

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix="aster_test_file_tools_"))

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)


# =====================================================================
# CONFIG
# =====================================================================

class TestConfigForma(FileToolsTestCase):
    """Solo validazione della forma grezza, dentro modules/config.py."""

    def _scrivi_config(self, extra: dict) -> Path:
        config = {
            "assistant": {"name": "Aster", "version": "0.0.0"},
            "ollama": {"model": "qwen3:8b", "host": "http://localhost:11434"},
            "chat": {"history_limit": 20, "stream": True},
            "memory": {"search_max_results": 5},
            "files": {"prompt": "prompt.txt", "memory": "data/memory.json"},
        }
        config.update(extra)

        percorso = self.base_dir / "config.json"
        percorso.write_text(json.dumps(config), encoding="utf-8")
        return percorso

    def test_allowed_roots_assente_nessun_errore(self):
        percorso = self._scrivi_config({})
        config = carica_config(percorso)

        self.assertNotIn("tools", config)

    def test_allowed_roots_vuota_nessun_errore(self):
        percorso = self._scrivi_config(
            {"tools": {"filesystem": {"allowed_roots": []}}}
        )
        config = carica_config(percorso)

        self.assertEqual(config["tools"]["filesystem"]["allowed_roots"], [])

    def test_allowed_roots_non_lista_errore(self):
        percorso = self._scrivi_config(
            {"tools": {"filesystem": {"allowed_roots": "C:/non/una/lista"}}}
        )

        with self.assertRaises(TypeError):
            carica_config(percorso)

    def test_allowed_roots_elemento_non_stringa_errore(self):
        percorso = self._scrivi_config(
            {"tools": {"filesystem": {"allowed_roots": [123]}}}
        )

        with self.assertRaises(TypeError):
            carica_config(percorso)


class TestPreparaAllowedRoots(FileToolsTestCase):

    def test_relativo_risolto_rispetto_a_base_dir_non_cwd(self):
        risultato = _prepara_allowed_roots(["./workspace"], self.base_dir)

        self.assertEqual(risultato, [self.base_dir / "workspace"])
        self.assertNotEqual(risultato[0], Path.cwd() / "workspace")

    def test_assoluto_resta_invariato(self):
        assoluto = self.base_dir / "altrove"
        risultato = _prepara_allowed_roots([str(assoluto)], self.base_dir)

        self.assertEqual(risultato, [assoluto])

    def test_prepara_contesto_filesystem_allowed_roots_assente(self):
        contesto = prepara_contesto_filesystem({}, self.base_dir)

        self.assertEqual(contesto.allowed_roots, [])


# =====================================================================
# REGISTRY
# =====================================================================

class TestRegistroFilesystem(unittest.TestCase):

    def setUp(self):
        self.registro = crea_registro_memoria()
        # Sistema non è nel file autorizzato per questo step, ma il
        # totale atteso lo presuppone: lo registriamo qui via import
        # diretto per verificare correttamente "10 tool totali".
        from modules.system_tools import registra_tool_sistema
        registra_tool_sistema(self.registro)
        registra_tool_filesystem(self.registro)

    def test_dominio_filesystem(self):
        tool_spec = self.registro.trova("list_directory")
        self.assertIsNotNone(tool_spec)
        self.assertEqual(tool_spec.dominio, "filesystem")

    def test_livello_read_only(self):
        tool_spec = self.registro.trova("list_directory")
        self.assertEqual(tool_spec.livello, "READ_ONLY")

    def test_totale_dieci_tool(self):
        nomi = {s["function"]["name"] for s in self.registro.elenco_schema()}
        self.assertEqual(len(nomi), 10)

    def test_sette_memory_invariati(self):
        nomi_memoria = {
            "cerca_memoria", "crea_memoria", "modifica_memoria",
            "elimina_memoria", "elimina_memoria_per_query",
            "ripristina_memoria", "gestisci_pending_memoria",
        }
        for nome in nomi_memoria:
            tool_spec = self.registro.trova(nome)
            self.assertIsNotNone(tool_spec, msg=nome)
            self.assertEqual(tool_spec.dominio, "memory", msg=nome)

    def test_due_system_invariati(self):
        for nome in ("get_system_info", "get_disk_usage"):
            tool_spec = self.registro.trova(nome)
            self.assertIsNotNone(tool_spec, msg=nome)
            self.assertEqual(tool_spec.dominio, "system", msg=nome)


# =====================================================================
# LIST_DIRECTORY: handler reale
# =====================================================================

class TestListDirectory(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.fuori = self.base_dir / "fuori"
        self.fuori.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def test_directory_autorizzata_success(self):
        (self.root / "a.txt").write_text("x")
        risultato = list_directory({"path": "."}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["status"], "success")

    def test_path_negato_access_denied(self):
        risultato = list_directory(
            {"path": str(self.fuori)}, self.contesto
        )

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "access_denied")

    def test_target_inesistente_not_found(self):
        risultato = list_directory(
            {"path": "non_esiste"}, self.contesto
        )

        self.assertEqual(risultato["status"], "not_found")

    def test_target_file_not_a_directory(self):
        (self.root / "file.txt").write_text("x")
        risultato = list_directory(
            {"path": "file.txt"}, self.contesto
        )

        self.assertEqual(risultato["status"], "not_a_directory")

    def test_directory_vuota(self):
        risultato = list_directory({"path": "."}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["data"]["entries"], [])
        self.assertFalse(risultato["data"]["truncated"])

    def test_singolo_livello_soltanto(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "nipote.txt").write_text("x")

        risultato = list_directory({"path": "."}, self.contesto)
        nomi = {e["name"] for e in risultato["data"]["entries"]}

        self.assertEqual(nomi, {"sub"})
        self.assertNotIn("nipote.txt", nomi)

    def test_max_100_entry(self):
        for indice in range(150):
            (self.root / f"file_{indice:03d}.txt").write_text("x")

        risultato = list_directory({"path": "."}, self.contesto)

        self.assertEqual(len(risultato["data"]["entries"]), MAX_DIRECTORY_ENTRIES)

    def test_oltre_100_truncated_true(self):
        for indice in range(150):
            (self.root / f"file_{indice:03d}.txt").write_text("x")

        risultato = list_directory({"path": "."}, self.contesto)

        self.assertTrue(risultato["data"]["truncated"])

    def test_ordering_casefold_deterministico(self):
        for nome in ("Banana.txt", "ananas.txt", "Ciliegia.txt", "arancia.txt"):
            (self.root / nome).write_text("x")

        risultato = list_directory({"path": "."}, self.contesto)
        nomi = [e["name"] for e in risultato["data"]["entries"]]

        self.assertEqual(
            nomi, sorted(nomi, key=lambda n: n.casefold())
        )

    def test_nessun_path_assoluto_in_output(self):
        (self.root / "a.txt").write_text("x")
        risultato = list_directory({"path": "."}, self.contesto)

        testo_risultato = json.dumps(risultato)
        self.assertNotIn(str(self.root), testo_risultato)
        self.assertNotIn(str(self.base_dir), testo_risultato)

    def test_nessun_campo_path_root_resolved_path(self):
        (self.root / "a.txt").write_text("x")
        risultato = list_directory({"path": "."}, self.contesto)

        self.assertNotIn("path", risultato)
        self.assertNotIn("path", risultato["data"])
        self.assertNotIn("root", risultato)
        self.assertNotIn("resolved_path", risultato)

    def test_symlink_interno_type_other(self):
        (self.root / "interno").mkdir()
        link = self.root / "link_interno"
        if not _symlink_supportato(self.root / "interno", link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = list_directory({"path": "."}, self.contesto)
        entry = next(
            e for e in risultato["data"]["entries"] if e["name"] == "link_interno"
        )
        self.assertEqual(entry["type"], "other")

    def test_symlink_esterno_type_other(self):
        link = self.root / "link_esterno"
        if not _symlink_supportato(self.fuori, link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = list_directory({"path": "."}, self.contesto)
        entry = next(
            e for e in risultato["data"]["entries"] if e["name"] == "link_esterno"
        )
        self.assertEqual(entry["type"], "other")

    def test_richiesta_tramite_symlink_esterno_access_denied(self):
        link = self.root / "link_esterno"
        if not _symlink_supportato(self.fuori, link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = list_directory(
            {"path": "link_esterno"}, self.contesto
        )

        self.assertEqual(risultato["status"], "access_denied")

    def test_eccezione_enumerazione_tool_error(self):
        (self.root / "a.txt").write_text("x")

        originale = Path.iterdir

        def iterdir_fallisce(self_path):
            raise OSError("errore simulato di enumerazione")

        Path.iterdir = iterdir_fallisce
        try:
            risultato = list_directory({"path": "."}, self.contesto)
        finally:
            Path.iterdir = originale

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "tool_error")
        # Il messaggio non deve mai contenere il path assoluto.
        self.assertNotIn(str(self.root), risultato.get("error", ""))


# =====================================================================
# TOCTOU
# =====================================================================

class TestToctou(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root_a = self.base_dir / "root_a"
        self.root_a.mkdir()
        (self.root_a / "solo_in_a.txt").write_text("x")

        self.root_b = self.base_dir / "root_b"
        self.root_b.mkdir()
        (self.root_b / "solo_in_b.txt").write_text("x")

        self.contesto = ContestoFilesystem(allowed_roots=[self.root_a])

    def test_usa_resolved_path_della_validazione_finale(self):
        chiamate = []

        def validazione_selettiva(path_richiesto, allowed_roots):
            chiamate.append(path_richiesto)
            if len(chiamate) == 1:
                return PathDecision(allowed=True, resolved_path=self.root_a, reason="ok")
            return PathDecision(allowed=True, resolved_path=self.root_b, reason="ok")

        originale = file_tools.risolvi_path_autorizzato
        file_tools.risolvi_path_autorizzato = validazione_selettiva
        try:
            risultato = list_directory({"path": "."}, self.contesto)
        finally:
            file_tools.risolvi_path_autorizzato = originale

        self.assertEqual(len(chiamate), 2)
        nomi = {e["name"] for e in risultato["data"]["entries"]}
        self.assertEqual(nomi, {"solo_in_b.txt"})

    def test_nessun_io_se_decisione_finale_nega(self):
        chiamate = []

        def validazione_selettiva(path_richiesto, allowed_roots):
            chiamate.append(path_richiesto)
            if len(chiamate) == 1:
                return PathDecision(allowed=True, resolved_path=self.root_a, reason="ok")
            return PathDecision(allowed=False, resolved_path=None, reason="outside_allowed_roots")

        originale_validazione = file_tools.risolvi_path_autorizzato
        file_tools.risolvi_path_autorizzato = validazione_selettiva

        originale_iterdir = Path.iterdir

        def iterdir_non_deve_essere_chiamato(self_path):
            raise AssertionError("iterdir non deve essere chiamato se la decisione finale nega.")

        Path.iterdir = iterdir_non_deve_essere_chiamato
        try:
            risultato = list_directory({"path": "."}, self.contesto)
        finally:
            file_tools.risolvi_path_autorizzato = originale_validazione
            Path.iterdir = originale_iterdir

        self.assertEqual(risultato["status"], "access_denied")


# =====================================================================
# FALLBACK
# =====================================================================

class TestFallbackDeterministicoFile(unittest.TestCase):

    def test_success(self):
        risultato = {
            "ok": True,
            "operation": "list_directory",
            "status": "success",
            "data": {
                "entries": [
                    {"name": "a.txt", "type": "file"},
                    {"name": "sub", "type": "directory"},
                ],
                "truncated": False,
            },
        }

        testo = fallback_deterministico_file(risultato)
        self.assertIn("a.txt", testo)
        self.assertIn("sub", testo)

    def test_success_vuota(self):
        risultato = {
            "ok": True,
            "status": "success",
            "data": {"entries": [], "truncated": False},
        }

        self.assertEqual(fallback_deterministico_file(risultato), "La directory è vuota.")

    def test_access_denied(self):
        risultato = {"ok": False, "status": "access_denied"}
        testo = fallback_deterministico_file(risultato)

        self.assertIn("accesso", testo.lower())

    def test_not_found(self):
        risultato = {"ok": False, "status": "not_found"}
        testo = fallback_deterministico_file(risultato)

        self.assertTrue(testo)

    def test_not_a_directory(self):
        risultato = {"ok": False, "status": "not_a_directory"}
        testo = fallback_deterministico_file(risultato)

        self.assertTrue(testo)

    def test_tool_error(self):
        risultato = {"ok": False, "status": "tool_error", "error": "Errore durante l'accesso alla directory."}
        testo = fallback_deterministico_file(risultato)

        self.assertTrue(testo)

    def test_nessun_fallback_espone_path_assoluto(self):
        for risultato in (
            {"ok": False, "status": "access_denied"},
            {"ok": False, "status": "not_found"},
            {"ok": False, "status": "not_a_directory"},
            {"ok": False, "status": "tool_error", "error": "Errore durante l'accesso alla directory."},
        ):
            with self.subTest(status=risultato["status"]):
                testo = fallback_deterministico_file(risultato)
                self.assertNotIn(":\\", testo)
                self.assertNotIn("/home/", testo)


# =====================================================================
# PIPELINE post-tool generica
# =====================================================================

class TestPipelineFilesystem(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        (self.root / "a.txt").write_text("x")
        contesto = ContestoFilesystem(allowed_roots=[self.root])
        self.risultato_tool = list_directory({"path": "."}, contesto)

    def test_secondo_giro_riuscito(self):
        stream = [_messaggio("Nella directory c'è a.txt.")]
        contatore = ContatoreChiamate(lambda: iter(stream))

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = contatore
        try:
            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[{"role": "tool", "content": "..."}],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=self.risultato_tool,
                salta_secondo_giro=False,
                fallback_deterministico=fallback_deterministico_file,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "Nella directory c'è a.txt.")
        self.assertEqual(contatore.chiamate, 1)

    def test_secondo_giro_fallito_usa_fallback(self):
        def esegui_risposta_finale_fallisce(*args, **kwargs):
            raise ConnectionError("Ollama non raggiungibile")

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = esegui_risposta_finale_fallisce
        try:
            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=self.risultato_tool,
                salta_secondo_giro=False,
                fallback_deterministico=fallback_deterministico_file,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertIn("a.txt", risposta)

    def test_nessun_retry_del_tool(self):
        contatore = ContatoreChiamate(lambda: iter([_messaggio("ok")]))

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = contatore
        try:
            genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=self.risultato_tool,
                salta_secondo_giro=False,
                fallback_deterministico=fallback_deterministico_file,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        # genera_risposta_post_tool non ha alcun riferimento al
        # registry/dispatch: strutturalmente non può rieseguire il
        # tool. Un solo giro Ollama, nessun secondo tentativo.
        self.assertEqual(contatore.chiamate, 1)


if __name__ == "__main__":
    unittest.main()
