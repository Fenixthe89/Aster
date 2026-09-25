"""
0.6.6a/0.6.6b - list_directory e read_file: test di regressione.

Verifica i tool filesystem reali di Aster: integrazione config (solo
forma grezza in modules/config.py, semantica in file_tools.py),
default deny, gli handler reali (containment via filesystem_policy.py,
invariato, doppia validazione TOCTOU), privacy dell'output (nessun
path in nessun risultato), classificazione symlink senza seguirne il
target per list_directory, filename/contenuto sensibile per read_file
(guard dedicato, indipendente da memory_tools.py), rilevamento
binario/testo, registrazione nel registry combinato, fallback
deterministico e pipeline post-tool generica (incluso il gate
sensitive_file che salta il secondo giro Ollama).

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
    MAX_READ_BYTES,
    ContestoFilesystem,
    _prepara_allowed_roots,
    fallback_deterministico_file,
    list_directory,
    prepara_contesto_filesystem,
    read_file,
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


def _symlink_file_supportato(sorgente: Path, link: Path) -> bool:
    try:
        link.symlink_to(sorgente, target_is_directory=False)
        return True
    except (OSError, NotImplementedError):
        return False


def _messaggio(contenuto: str):
    return SimpleNamespace(message=SimpleNamespace(content=contenuto))


class ContatoreChiamate:
    def __init__(self, comportamento):
        self.chiamate = 0
        self._comportamento = comportamento

    def __call__(self, modello, messaggi, host_ollama, timeout_ollama, num_ctx):
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
        for nome in ("list_directory", "read_file"):
            tool_spec = self.registro.trova(nome)
            self.assertIsNotNone(tool_spec, msg=nome)
            self.assertEqual(tool_spec.dominio, "filesystem", msg=nome)

    def test_livello_read_only(self):
        for nome in ("list_directory", "read_file"):
            tool_spec = self.registro.trova(nome)
            self.assertEqual(tool_spec.livello, "READ_ONLY", msg=nome)

    def test_totale_dodici_tool(self):
        nomi = {s["function"]["name"] for s in self.registro.elenco_schema()}
        self.assertEqual(len(nomi), 12)
        self.assertIn("list_directory", nomi)
        self.assertIn("read_file", nomi)

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


# =====================================================================
# READ_FILE: base (successo, encoding, dimensione, binario)
# =====================================================================

# Byte binari sintetici: validi come UTF-8/cp1252 (tutti < 0x80, quindi
# decodificano senza eccezioni), ma pieni di caratteri di controllo non
# ammessi. Servono a dimostrare che il text-likeness intercetta un
# binario anche quando la decodifica NON solleva alcuna eccezione.
_BYTES_BINARI_SENZA_NUL = bytes([1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15, 16, 17, 18, 19, 20] * 50)


class TestReadFileBase(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.fuori = self.base_dir / "fuori"
        self.fuori.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def test_success_utf8(self):
        (self.root / "a.txt").write_text("Ciao mondo àèìòù", encoding="utf-8")

        risultato = read_file({"path": "a.txt"}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["status"], "success")
        self.assertEqual(risultato["data"]["content"], "Ciao mondo àèìòù")
        self.assertEqual(risultato["data"]["encoding"], "utf-8")

    def test_success_utf8_bom(self):
        dati = b"\xef\xbb\xbf" + "Ciao con BOM".encode("utf-8")
        (self.root / "bom.txt").write_bytes(dati)

        risultato = read_file({"path": "bom.txt"}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["data"]["content"], "Ciao con BOM")
        self.assertEqual(risultato["data"]["encoding"], "utf-8")

    def test_success_cp1252(self):
        # "caffè e tè" in cp1252 grezzo: non è UTF-8 valido.
        dati = "caffè e tè".encode("cp1252")
        (self.root / "cp1252.txt").write_bytes(dati)

        risultato = read_file({"path": "cp1252.txt"}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["data"]["encoding"], "cp1252")
        self.assertIn("caff", risultato["data"]["content"])

    def test_file_vuoto(self):
        (self.root / "vuoto.txt").write_bytes(b"")

        risultato = read_file({"path": "vuoto.txt"}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["data"]["content"], "")

    def test_denied(self):
        (self.fuori / "segreto.txt").write_text("x")

        risultato = read_file({"path": str(self.fuori / "segreto.txt")}, self.contesto)

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "access_denied")

    def test_not_found(self):
        risultato = read_file({"path": "non_esiste.txt"}, self.contesto)

        self.assertEqual(risultato["status"], "not_found")

    def test_directory_not_a_file(self):
        (self.root / "sub").mkdir()

        risultato = read_file({"path": "sub"}, self.contesto)

        self.assertEqual(risultato["status"], "not_a_file")

    def test_esattamente_64kib_consentito(self):
        (self.root / "esatto.txt").write_bytes(b"a" * MAX_READ_BYTES)

        risultato = read_file({"path": "esatto.txt"}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(len(risultato["data"]["content"]), MAX_READ_BYTES)

    def test_oltre_64kib_too_large(self):
        (self.root / "grande.txt").write_bytes(b"a" * (MAX_READ_BYTES + 1))

        risultato = read_file({"path": "grande.txt"}, self.contesto)

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "too_large")

    def test_too_large_non_contiene_content(self):
        (self.root / "grande.txt").write_bytes(b"a" * (MAX_READ_BYTES + 1))

        risultato = read_file({"path": "grande.txt"}, self.contesto)

        self.assertNotIn("data", risultato)
        self.assertNotIn("content", risultato)

    def test_nul_binary_file(self):
        (self.root / "bin.dat").write_bytes(b"testo\x00con\x00nul")

        risultato = read_file({"path": "bin.dat"}, self.contesto)

        self.assertEqual(risultato["status"], "binary_file")

    def test_binario_senza_nul_intercettato_da_text_likeness(self):
        (self.root / "bin2.dat").write_bytes(_BYTES_BINARI_SENZA_NUL)

        risultato = read_file({"path": "bin2.dat"}, self.contesto)

        self.assertEqual(risultato["status"], "binary_file")

    def test_binario_utf8_valido_con_controlli_intercettato(self):
        # Gli stessi byte sono validi UTF-8 (< 0x80): la decodifica
        # utf-8-sig riesce, ma il text-likeness deve comunque respingerli.
        dati = _BYTES_BINARI_SENZA_NUL
        dati.decode("utf-8-sig")  # sanity: non solleva eccezioni
        (self.root / "bin3.dat").write_bytes(dati)

        risultato = read_file({"path": "bin3.dat"}, self.contesto)

        self.assertEqual(risultato["status"], "binary_file")

    def test_nessun_path_assoluto_in_output(self):
        (self.root / "a.txt").write_text("contenuto")
        risultato = read_file({"path": "a.txt"}, self.contesto)

        testo_risultato = json.dumps(risultato)
        self.assertNotIn(str(self.root), testo_risultato)
        self.assertNotIn(str(self.base_dir), testo_risultato)


# =====================================================================
# READ_FILE: nome file sensibile
# =====================================================================

class TestReadFileNomeSensibile(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def _crea_e_leggi(self, nome_file: str) -> dict:
        (self.root / nome_file).write_text("contenuto qualsiasi")
        return read_file({"path": nome_file}, self.contesto)

    def test_env(self):
        self.assertEqual(self._crea_e_leggi(".env")["status"], "sensitive_file")

    def test_env_local(self):
        self.assertEqual(self._crea_e_leggi(".env.local")["status"], "sensitive_file")

    def test_id_rsa(self):
        self.assertEqual(self._crea_e_leggi("id_rsa")["status"], "sensitive_file")

    def test_id_rsa_pub_consentito(self):
        risultato = self._crea_e_leggi("id_rsa.pub")
        self.assertEqual(risultato["status"], "success")

    def test_id_ed25519(self):
        self.assertEqual(self._crea_e_leggi("id_ed25519")["status"], "sensitive_file")

    def test_estensione_pem(self):
        self.assertEqual(self._crea_e_leggi("cert.pem")["status"], "sensitive_file")

    def test_estensione_key(self):
        self.assertEqual(self._crea_e_leggi("server.key")["status"], "sensitive_file")

    def test_estensione_pfx(self):
        self.assertEqual(self._crea_e_leggi("bundle.pfx")["status"], "sensitive_file")

    def test_estensione_p12(self):
        self.assertEqual(self._crea_e_leggi("bundle.p12")["status"], "sensitive_file")

    def test_credentials_json(self):
        self.assertEqual(self._crea_e_leggi("credentials.json")["status"], "sensitive_file")

    def test_secrets_yaml(self):
        self.assertEqual(self._crea_e_leggi("secrets.yaml")["status"], "sensitive_file")

    def test_case_insensitive(self):
        self.assertEqual(self._crea_e_leggi(".ENV")["status"], "sensitive_file")

    def test_file_normale_consentito(self):
        risultato = self._crea_e_leggi("README.md")
        self.assertEqual(risultato["status"], "success")


# =====================================================================
# READ_FILE: contenuto sensibile
# =====================================================================

class TestReadFileContenutoSensibile(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def _scrivi_e_leggi(self, contenuto: str) -> dict:
        # Nome sempre innocuo: qui testiamo il guard sul CONTENUTO, non
        # sul nome (già coperto da TestReadFileNomeSensibile).
        (self.root / "notes.txt").write_text(contenuto, encoding="utf-8")
        return read_file({"path": "notes.txt"}, self.contesto)

    def test_pem_rsa_private_key(self):
        contenuto = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBOgIBAAJBAK...\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        self.assertEqual(self._scrivi_e_leggi(contenuto)["status"], "sensitive_file")

    def test_pem_openssh_private_key(self):
        contenuto = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEA...\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        self.assertEqual(self._scrivi_e_leggi(contenuto)["status"], "sensitive_file")

    def test_password_concreta(self):
        self.assertEqual(
            self._scrivi_e_leggi('password = "SuperSegreta123"')["status"],
            "sensitive_file",
        )

    def test_client_secret_concreto(self):
        self.assertEqual(
            self._scrivi_e_leggi('CLIENT_SECRET = "abc123456789xyz"')["status"],
            "sensitive_file",
        )

    def test_secret_key_concreto(self):
        self.assertEqual(
            self._scrivi_e_leggi('SECRET_KEY = "django-insecure-abc123xyzDEF456"')["status"],
            "sensitive_file",
        )

    def test_api_key_concreta(self):
        self.assertEqual(
            self._scrivi_e_leggi('api_key = "qualcosa-di-concreto-123"')["status"],
            "sensitive_file",
        )

    def test_token_prefisso_credibile(self):
        self.assertEqual(
            self._scrivi_e_leggi("api_key = 'sk-abcdef1234567890abcdef'")["status"],
            "sensitive_file",
        )

    def test_password_input_non_bloccata(self):
        risultato = self._scrivi_e_leggi('password = input("Password: ")')
        self.assertEqual(risultato["status"], "success")

    def test_api_key_name_costante_non_bloccata(self):
        risultato = self._scrivi_e_leggi('API_KEY_NAME = "OPENAI_API_KEY"')
        self.assertEqual(risultato["status"], "success")

    def test_get_password_non_bloccato(self):
        risultato = self._scrivi_e_leggi('config["password"] = get_password()')
        self.assertEqual(risultato["status"], "success")

    def test_token_none_non_bloccato(self):
        risultato = self._scrivi_e_leggi("if token is None:\n    pass\n")
        self.assertEqual(risultato["status"], "success")

    def test_placeholder_angolari_non_bloccato(self):
        risultato = self._scrivi_e_leggi('password = "<your password>"')
        self.assertEqual(risultato["status"], "success")

    def test_your_password_non_bloccato(self):
        risultato = self._scrivi_e_leggi('API_KEY = "YOUR_API_KEY"')
        self.assertEqual(risultato["status"], "success")

    def test_template_dollaro_non_bloccato(self):
        risultato = self._scrivi_e_leggi('token = "${TOKEN}"')
        self.assertEqual(risultato["status"], "success")

    def test_changeme_non_bloccato(self):
        risultato = self._scrivi_e_leggi('secret = "changeme"')
        self.assertEqual(risultato["status"], "success")


# =====================================================================
# READ_FILE: symlink
# =====================================================================

class TestReadFileSymlink(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.fuori = self.base_dir / "fuori"
        self.fuori.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def test_symlink_interno_verso_file_interno_consentito(self):
        target = self.root / "reale.txt"
        target.write_text("contenuto interno")

        link = self.root / "alias.txt"
        if not _symlink_file_supportato(target, link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = read_file({"path": "alias.txt"}, self.contesto)

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["data"]["content"], "contenuto interno")

    def test_symlink_interno_verso_esterno_access_denied(self):
        target = self.fuori / "segreto.txt"
        target.write_text("dato esterno")

        link = self.root / "alias.txt"
        if not _symlink_file_supportato(target, link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = read_file({"path": "alias.txt"}, self.contesto)

        self.assertEqual(risultato["status"], "access_denied")

    def test_symlink_nome_innocuo_verso_env_sensitive_file(self):
        target = self.root / ".env"
        target.write_text("SEGRETO=reale")

        link = self.root / "innocuo.txt"
        if not _symlink_file_supportato(target, link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = read_file({"path": "innocuo.txt"}, self.contesto)

        self.assertEqual(risultato["status"], "sensitive_file")

    def test_symlink_nome_env_verso_file_normale_consentito(self):
        target = self.root / "normale.txt"
        target.write_text("nulla di sensibile qui")

        link = self.root / ".env"
        if not _symlink_file_supportato(target, link):
            self.skipTest("symlink non supportato su questa piattaforma")

        risultato = read_file({"path": ".env"}, self.contesto)

        # Decisione motivata: il controllo e' sul resolved_path.name
        # (il target reale, "normale.txt"), non sul nome richiesto: il
        # dato realmente esposto non e' sensibile.
        self.assertEqual(risultato["status"], "success")
        self.assertEqual(risultato["data"]["content"], "nulla di sensibile qui")


# =====================================================================
# READ_FILE: TOCTOU
# =====================================================================

class TestReadFileToctou(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root_a = self.base_dir / "root_a"
        self.root_a.mkdir()
        (self.root_a / "solo_in_a.txt").write_text("contenuto A")

        self.root_b = self.base_dir / "root_b"
        self.root_b.mkdir()
        (self.root_b / "solo_in_b.txt").write_text("contenuto B")

        self.contesto = ContestoFilesystem(allowed_roots=[self.root_a])

    def test_usa_resolved_path_della_validazione_finale(self):
        chiamate = []

        def validazione_selettiva(path_richiesto, allowed_roots):
            chiamate.append(path_richiesto)
            if len(chiamate) == 1:
                return PathDecision(
                    allowed=True,
                    resolved_path=self.root_a / "solo_in_a.txt",
                    reason="ok",
                )
            return PathDecision(
                allowed=True,
                resolved_path=self.root_b / "solo_in_b.txt",
                reason="ok",
            )

        originale = file_tools.risolvi_path_autorizzato
        file_tools.risolvi_path_autorizzato = validazione_selettiva
        try:
            risultato = read_file({"path": "qualsiasi.txt"}, self.contesto)
        finally:
            file_tools.risolvi_path_autorizzato = originale

        self.assertEqual(len(chiamate), 2)
        self.assertEqual(risultato["data"]["content"], "contenuto B")

    def test_nessun_open_se_decisione_finale_nega(self):
        chiamate = []

        def validazione_selettiva(path_richiesto, allowed_roots):
            chiamate.append(path_richiesto)
            if len(chiamate) == 1:
                return PathDecision(
                    allowed=True,
                    resolved_path=self.root_a / "solo_in_a.txt",
                    reason="ok",
                )
            return PathDecision(allowed=False, resolved_path=None, reason="outside_allowed_roots")

        originale_validazione = file_tools.risolvi_path_autorizzato
        file_tools.risolvi_path_autorizzato = validazione_selettiva

        originale_open = Path.open

        def open_non_deve_essere_chiamato(self_path, *args, **kwargs):
            raise AssertionError("open() non deve essere chiamato se la decisione finale nega.")

        Path.open = open_non_deve_essere_chiamato
        try:
            risultato = read_file({"path": "qualsiasi.txt"}, self.contesto)
        finally:
            file_tools.risolvi_path_autorizzato = originale_validazione
            Path.open = originale_open

        self.assertEqual(risultato["status"], "access_denied")


# =====================================================================
# READ_FILE: sicurezza del risultato
# =====================================================================

class TestReadFileResultSecurity(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def test_sensitive_file_non_contiene_content(self):
        (self.root / ".env").write_text("SEGRETO=xyz")
        risultato = read_file({"path": ".env"}, self.contesto)

        self.assertNotIn("data", risultato)
        self.assertNotIn("content", risultato)

    def test_sensitive_file_non_contiene_match_category(self):
        (self.root / "notes.txt").write_text('password = "SuperSegreta123"')
        risultato = read_file({"path": "notes.txt"}, self.contesto)

        testo = json.dumps(risultato)
        self.assertNotIn("SuperSegreta123", testo)
        self.assertNotIn("category", risultato)
        self.assertNotIn("match", risultato)
        self.assertEqual(
            set(risultato.keys()), {"ok", "operation", "status"}
        )

    def test_tool_error_non_contiene_path_assoluto(self):
        (self.root / "a.txt").write_text("x")

        originale = Path.open

        def open_fallisce(self_path, *args, **kwargs):
            raise OSError(f"errore simulato su {self_path}")

        Path.open = open_fallisce
        try:
            risultato = read_file({"path": "a.txt"}, self.contesto)
        finally:
            Path.open = originale

        self.assertEqual(risultato["status"], "tool_error")
        self.assertNotIn(str(self.root), risultato.get("error", ""))


# =====================================================================
# PIPELINE: sicurezza end-to-end per sensitive_file
# =====================================================================

class TestReadFilePipelineSicurezza(FileToolsTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.contesto = ContestoFilesystem(allowed_roots=[self.root])

    def test_read_success_arriva_a_secondo_giro(self):
        (self.root / "a.txt").write_text("contenuto pubblico")
        risultato_tool = read_file({"path": "a.txt"}, self.contesto)

        stream = [_messaggio("Il file contiene: contenuto pubblico")]
        contatore = ContatoreChiamate(lambda: iter(stream))

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = contatore
        try:
            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[{"role": "tool", "content": "..."}],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=(risultato_tool.get("status") == "sensitive_file"),
                fallback_deterministico=fallback_deterministico_file,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(contatore.chiamate, 1)
        self.assertIn("contenuto pubblico", risposta)

    def test_sensitive_file_secondo_giro_non_chiamato(self):
        (self.root / ".env").write_text("PASSWORD=SegretaReale123")
        risultato_tool = read_file({"path": ".env"}, self.contesto)
        self.assertEqual(risultato_tool["status"], "sensitive_file")

        def esegui_risposta_finale_non_deve_essere_chiamata(*args, **kwargs):
            raise AssertionError(
                "Il secondo giro Ollama non deve essere invocato per sensitive_file."
            )

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = esegui_risposta_finale_non_deve_essere_chiamata
        try:
            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[{"role": "tool", "content": json.dumps(risultato_tool)}],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=(risultato_tool.get("status") == "sensitive_file"),
                fallback_deterministico=fallback_deterministico_file,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertNotIn("SegretaReale123", risposta)
        self.assertIn("sensibil", risposta.lower())

    def test_role_tool_serializzato_non_contiene_secret(self):
        (self.root / "config.py").write_text('PASSWORD = "SegretaReale123"')
        risultato_tool = read_file({"path": "config.py"}, self.contesto)
        self.assertEqual(risultato_tool["status"], "sensitive_file")

        # Simula esattamente ciò che chat.py mette in role="tool".
        messaggio_tool = json.dumps(risultato_tool, ensure_ascii=False)

        self.assertNotIn("SegretaReale123", messaggio_tool)

    def test_no_retry_su_sensitive_file(self):
        (self.root / ".env").write_text("X=1")
        risultato_tool = read_file({"path": ".env"}, self.contesto)

        chiamate_fallback = []

        def fallback_spia(risultato):
            chiamate_fallback.append(risultato)
            return fallback_deterministico_file(risultato)

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("non deve essere chiamato")
        )
        try:
            genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=True,
                fallback_deterministico=fallback_spia,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        # fallback chiamato esattamente una volta: nessun retry.
        self.assertEqual(len(chiamate_fallback), 1)

    def test_fallback_sensitive_e_neutro(self):
        risultato_tool = {"ok": False, "operation": "read_file", "status": "sensitive_file"}
        testo = fallback_deterministico_file(risultato_tool)

        self.assertIn("sensibil", testo.lower())
        self.assertNotIn(":\\", testo)
        self.assertNotIn("/home/", testo)


if __name__ == "__main__":
    unittest.main()
