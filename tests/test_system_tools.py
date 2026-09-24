"""
0.6.3/0.6.4 - get_system_info e get_disk_usage: test di regressione.

Verifica i tool non-memory reali di Aster: schema, registrazione nel
RegistroStrumenti (dominio "system", livello READ_ONLY) accanto ai 7
tool memoria esistenti, handler reali (nessun mock del sistema
operativo salvo per i casi limite esplicitamente indicati: legge i
valori veri della macchina che esegue i test), assenza di dati
personali, e il fallback deterministico del dominio (router per
"operation", un ramo per ciascun tool, nessun dump generico).

Copre anche la pipeline post-tool generica (modules/tool_response.py,
invariata) applicata a risultati "system" reali di entrambi i tool.

Nessun file in data/ viene mai letto o scritto. Nessuna chiamata a
Ollama: esegui_risposta_finale viene sempre sostituita con un doppio
di test.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.system_tools as system_tools
import modules.tool_response as tool_response
from modules.system_tools import (
    TOOLS_SISTEMA,
    fallback_deterministico_sistema,
    get_disk_usage,
    get_system_info,
    registra_tool_sistema,
)
from modules.tool_registry import RegistroStrumenti, crea_registro_memoria
from modules.tool_response import genera_risposta_post_tool

CAMPI_ATTESI_SYSTEM_INFO = {
    "os_name",
    "os_release",
    "machine",
    "python_version",
    "cpu_count",
    "processor",
}

CAMPI_ATTESI_DISK_USAGE = {
    "scope",
    "total_bytes",
    "used_bytes",
    "free_bytes",
}

CHIAVI_VIETATE = {
    "hostname",
    "node",
    "username",
    "user",
    "home",
    "home_dir",
    "ip",
    "ip_address",
    "mac",
    "mac_address",
    "path",
    "guid",
    "machine_guid",
    "serial",
    "serial_number",
    "mount",
    "mountpoint",
    "drive",
    "volume",
    "cwd",
}


def _schema_per_nome(nome: str) -> dict:
    for schema in TOOLS_SISTEMA:
        if schema["function"]["name"] == nome:
            return schema
    raise AssertionError(f"Schema non trovato in TOOLS_SISTEMA: {nome}")


def _messaggio(contenuto: str):
    return SimpleNamespace(message=SimpleNamespace(content=contenuto))


class ContatoreChiamate:
    def __init__(self, comportamento):
        self.chiamate = 0
        self._comportamento = comportamento

    def __call__(self, modello, messaggi, host_ollama, timeout_ollama):
        self.chiamate += 1
        return self._comportamento()


# =====================================================================
# Schema
# =====================================================================

class TestToolsSistemaElenco(unittest.TestCase):

    def test_contiene_esattamente_due_schemi(self):
        nomi = {schema["function"]["name"] for schema in TOOLS_SISTEMA}
        self.assertEqual(nomi, {"get_system_info", "get_disk_usage"})


class TestSchemaGetSystemInfo(unittest.TestCase):

    def test_schema_valido(self):
        schema = _schema_per_nome("get_system_info")

        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "get_system_info")
        self.assertIsInstance(schema["function"]["description"], str)
        self.assertTrue(schema["function"]["description"].strip())

    def test_nessun_parametro_richiesto(self):
        parametri = _schema_per_nome("get_system_info")["function"]["parameters"]

        self.assertEqual(parametri.get("properties"), {})
        self.assertEqual(parametri.get("required"), [])


class TestSchemaGetDiskUsage(unittest.TestCase):

    def test_schema_valido(self):
        schema = _schema_per_nome("get_disk_usage")

        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "get_disk_usage")
        self.assertIsInstance(schema["function"]["description"], str)
        self.assertTrue(schema["function"]["description"].strip())

    def test_nessun_parametro_richiesto(self):
        parametri = _schema_per_nome("get_disk_usage")["function"]["parameters"]

        self.assertEqual(parametri.get("properties"), {})
        self.assertEqual(parametri.get("required"), [])


# =====================================================================
# Registrazione nel registry combinato memoria + sistema
# =====================================================================

class TestRegistrazioneSistema(unittest.TestCase):

    def setUp(self):
        self.registro = crea_registro_memoria()
        registra_tool_sistema(self.registro)

    def test_dominio_system(self):
        for nome in ("get_system_info", "get_disk_usage"):
            tool_spec = self.registro.trova(nome)
            self.assertIsNotNone(tool_spec, msg=nome)
            self.assertEqual(tool_spec.dominio, "system", msg=nome)

    def test_livello_read_only(self):
        for nome in ("get_system_info", "get_disk_usage"):
            tool_spec = self.registro.trova(nome)
            self.assertEqual(tool_spec.livello, "READ_ONLY", msg=nome)

    def test_registro_combinato_contiene_memoria_e_sistema(self):
        nomi = {
            schema["function"]["name"]
            for schema in self.registro.elenco_schema()
        }

        nomi_memoria_attesi = {
            "cerca_memoria",
            "crea_memoria",
            "modifica_memoria",
            "elimina_memoria",
            "elimina_memoria_per_query",
            "ripristina_memoria",
            "gestisci_pending_memoria",
        }

        self.assertTrue(nomi_memoria_attesi.issubset(nomi))
        self.assertIn("get_system_info", nomi)
        self.assertIn("get_disk_usage", nomi)
        self.assertEqual(len(nomi), 9)

    def test_nessun_tool_memoria_alterato(self):
        for nome in (
            "cerca_memoria",
            "crea_memoria",
            "modifica_memoria",
            "elimina_memoria",
            "elimina_memoria_per_query",
            "ripristina_memoria",
            "gestisci_pending_memoria",
        ):
            tool_spec = self.registro.trova(nome)
            self.assertIsNotNone(tool_spec)
            self.assertEqual(tool_spec.dominio, "memory")

    def test_tool_sconosciuto_invariato(self):
        risultato = self.registro.dispatch("tool_fantasma", {}, None)

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "tool_error")
        self.assertIsNone(risultato["domain"])


# =====================================================================
# Handler reale (nessun mock: valori veri della macchina di test)
# =====================================================================

class TestHandlerGetSystemInfo(unittest.TestCase):

    def setUp(self):
        self.risultato = get_system_info({}, None)

    def test_successo(self):
        self.assertTrue(self.risultato["ok"])
        self.assertEqual(self.risultato["status"], "success")

    def test_operation_corretta(self):
        self.assertEqual(self.risultato["operation"], "get_system_info")

    def test_tutti_i_campi_presenti(self):
        data = self.risultato["data"]
        self.assertEqual(set(data.keys()), CAMPI_ATTESI_SYSTEM_INFO)

    def test_tipi_corretti(self):
        data = self.risultato["data"]

        for campo in ("os_name", "os_release", "machine", "python_version", "processor"):
            self.assertIsInstance(data[campo], str, msg=campo)

        self.assertTrue(data["cpu_count"] is None or isinstance(data["cpu_count"], int))

    def test_nessun_dato_personale(self):
        data = self.risultato["data"]

        chiavi_presenti = {chiave.lower() for chiave in data.keys()}
        self.assertEqual(chiavi_presenti & CHIAVI_VIETATE, set())

        # Anche i valori non devono contenere la home directory reale.
        home = str(Path.home())
        for valore in data.values():
            if isinstance(valore, str):
                self.assertNotIn(home, valore)

    def test_sorgente_senza_path_hardcoded_personali(self):
        sorgente = (BASE_DIR / "modules" / "system_tools.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("C:\\Aster", sorgente)
        self.assertNotIn("C:/Aster", sorgente)
        self.assertNotIn(str(Path.home()), sorgente)


class TestHandlerCasiLimite(unittest.TestCase):
    """processor="" e cpu_count=None sono esiti validi, non errori."""

    def test_processor_vuoto_consentito(self):
        originale = system_tools.platform.processor
        system_tools.platform.processor = lambda: ""
        try:
            risultato = get_system_info({}, None)
        finally:
            system_tools.platform.processor = originale

        self.assertTrue(risultato["ok"])
        self.assertEqual(risultato["data"]["processor"], "")

    def test_cpu_count_none_consentito(self):
        originale = system_tools.os.cpu_count
        system_tools.os.cpu_count = lambda: None
        try:
            risultato = get_system_info({}, None)
        finally:
            system_tools.os.cpu_count = originale

        self.assertTrue(risultato["ok"])
        self.assertIsNone(risultato["data"]["cpu_count"])


# =====================================================================
# Fallback deterministico del dominio system
# =====================================================================

class TestFallbackDeterministicoSistema(unittest.TestCase):

    def test_successo_leggibile(self):
        risultato_tool = {
            "ok": True,
            "operation": "get_system_info",
            "status": "success",
            "data": {
                "os_name": "Windows",
                "os_release": "11",
                "machine": "AMD64",
                "python_version": "3.12.4",
                "cpu_count": 24,
                "processor": "AMD64 Family 25 Model 33 Stepping 0, AuthenticAMD",
            },
        }

        testo = fallback_deterministico_sistema(risultato_tool)

        self.assertIn("Windows", testo)
        self.assertIn("11", testo)
        self.assertIn("AMD64", testo)
        self.assertIn("24", testo)
        self.assertIn("3.12.4", testo)

    def test_errore(self):
        risultato_tool = {
            "ok": False,
            "operation": "get_system_info",
            "status": "tool_error",
            "error": "errore di prova",
        }

        self.assertEqual(
            fallback_deterministico_sistema(risultato_tool),
            "errore di prova",
        )

    def test_errore_senza_messaggio(self):
        risultato_tool = {
            "ok": False,
            "operation": "get_system_info",
            "status": "tool_error",
        }

        testo = fallback_deterministico_sistema(risultato_tool)
        self.assertTrue(testo)
        self.assertNotIn("memoria", testo.lower())
        self.assertNotIn("ricordo", testo.lower())

    def test_gestisce_processor_vuoto(self):
        risultato_tool = {
            "ok": True,
            "operation": "get_system_info",
            "status": "success",
            "data": {
                "os_name": "Linux",
                "os_release": "6.8.0",
                "machine": "x86_64",
                "python_version": "3.12.4",
                "cpu_count": 8,
                "processor": "",
            },
        }

        testo = fallback_deterministico_sistema(risultato_tool)
        self.assertIn("non disponibile", testo)
        self.assertNotIn("Processore: \n", testo)

    def test_gestisce_cpu_count_none(self):
        risultato_tool = {
            "ok": True,
            "operation": "get_system_info",
            "status": "success",
            "data": {
                "os_name": "Linux",
                "os_release": "6.8.0",
                "machine": "x86_64",
                "python_version": "3.12.4",
                "cpu_count": None,
                "processor": "generic",
            },
        }

        testo = fallback_deterministico_sistema(risultato_tool)
        self.assertIn("CPU logiche: non disponibile", testo)

    def test_nessun_messaggio_memoria(self):
        risultato_tool = {
            "ok": True,
            "operation": "get_system_info",
            "status": "success",
            "data": {
                "os_name": "Windows",
                "os_release": "11",
                "machine": "AMD64",
                "python_version": "3.12.4",
                "cpu_count": 24,
                "processor": "x",
            },
        }

        testo = fallback_deterministico_sistema(risultato_tool)
        self.assertNotIn("ricordo", testo.lower())
        self.assertNotIn("memoria", testo.lower())

    def test_disk_usage_successo_leggibile(self):
        risultato_tool = {
            "ok": True,
            "operation": "get_disk_usage",
            "status": "success",
            "data": {
                "scope": "aster_filesystem",
                "total_bytes": 2 * 1024 ** 4,
                "used_bytes": 1 * 1024 ** 4,
                "free_bytes": 1 * 1024 ** 4,
            },
        }

        testo = fallback_deterministico_sistema(risultato_tool)

        self.assertIn("TiB", testo)
        self.assertIn("Spazio totale", testo)
        self.assertIn("Spazio usato", testo)
        self.assertIn("Spazio libero", testo)

    def test_disk_usage_errore(self):
        risultato_tool = {
            "ok": False,
            "operation": "get_disk_usage",
            "status": "tool_error",
            "error": "errore disco di prova",
        }

        self.assertEqual(
            fallback_deterministico_sistema(risultato_tool),
            "errore disco di prova",
        )

    def test_disk_usage_non_confonde_campi_con_system_info(self):
        risultato_tool = {
            "ok": True,
            "operation": "get_disk_usage",
            "status": "success",
            "data": {
                "scope": "aster_filesystem",
                "total_bytes": 500 * 1024 ** 3,
                "used_bytes": 200 * 1024 ** 3,
                "free_bytes": 300 * 1024 ** 3,
            },
        }

        testo = fallback_deterministico_sistema(risultato_tool)
        self.assertNotIn("Sistema operativo", testo)
        self.assertNotIn("CPU logiche", testo)

    def test_operation_sconosciuta_usa_fallback_generico_non_dump(self):
        risultato_tool = {
            "ok": True,
            "operation": "un_tool_futuro_non_ancora_esistente",
            "status": "success",
            "data": {"qualcosa": 1},
        }

        testo = fallback_deterministico_sistema(risultato_tool)
        self.assertTrue(testo)
        self.assertNotIn("Sistema operativo", testo)
        self.assertNotIn("Spazio totale", testo)
        self.assertNotIn("qualcosa", testo)


# =====================================================================
# Handler reale get_disk_usage (nessun mock salvo dove indicato)
# =====================================================================

class TestHandlerGetDiskUsage(unittest.TestCase):

    def setUp(self):
        self.risultato = get_disk_usage({}, None)

    def test_successo(self):
        self.assertTrue(self.risultato["ok"])
        self.assertEqual(self.risultato["status"], "success")

    def test_operation_corretta(self):
        self.assertEqual(self.risultato["operation"], "get_disk_usage")

    def test_tutti_i_campi_presenti(self):
        data = self.risultato["data"]
        self.assertEqual(set(data.keys()), CAMPI_ATTESI_DISK_USAGE)

    def test_scope_fisso(self):
        self.assertEqual(self.risultato["data"]["scope"], "aster_filesystem")

    def test_tipi_e_valori_non_negativi(self):
        data = self.risultato["data"]
        for campo in ("total_bytes", "used_bytes", "free_bytes"):
            self.assertIsInstance(data[campo], int, msg=campo)
            self.assertGreaterEqual(data[campo], 0, msg=campo)

    def test_total_maggiore_o_uguale_used_e_free(self):
        data = self.risultato["data"]
        self.assertGreaterEqual(data["total_bytes"], data["used_bytes"])
        self.assertGreaterEqual(data["total_bytes"], data["free_bytes"])

    def test_nessun_dato_personale(self):
        data = self.risultato["data"]

        chiavi_presenti = {chiave.lower() for chiave in data.keys()}
        self.assertEqual(chiavi_presenti & CHIAVI_VIETATE, set())

        home = str(Path.home())
        for valore in data.values():
            if isinstance(valore, str):
                self.assertNotIn(home, valore)

    def test_scope_non_e_un_mount_o_drive_root(self):
        scope = self.risultato["data"]["scope"]
        self.assertEqual(scope, "aster_filesystem")
        self.assertNotIn(":", scope)
        self.assertNotIn("\\", scope)
        self.assertNotIn("/", scope)

    def test_path_passato_a_disk_usage_e_la_directory_del_modulo_non_lanchor(self):
        percorso_catturato = []
        originale = system_tools.shutil.disk_usage

        def disk_usage_spia(percorso):
            percorso_catturato.append(Path(percorso))
            return originale(percorso)

        system_tools.shutil.disk_usage = disk_usage_spia
        try:
            get_disk_usage({}, None)
        finally:
            system_tools.shutil.disk_usage = originale

        self.assertEqual(len(percorso_catturato), 1)

        percorso_reale = Path(system_tools.__file__).resolve().parent
        self.assertEqual(percorso_catturato[0], percorso_reale)

        # La directory reale del modulo non e' semplicemente l'anchor
        # (drive root): in questo repository modules/ e' annidata
        # sotto la radice del volume, quindi i due path devono
        # differire (la correzione richiesta rispetto a .anchor).
        self.assertNotEqual(
            percorso_catturato[0],
            Path(percorso_catturato[0].anchor),
        )

    def test_eccezione_produce_tool_error(self):
        def disk_usage_fallisce(percorso):
            raise OSError("disco non raggiungibile")

        originale = system_tools.shutil.disk_usage
        system_tools.shutil.disk_usage = disk_usage_fallisce
        try:
            risultato = get_disk_usage({}, None)
        finally:
            system_tools.shutil.disk_usage = originale

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["operation"], "get_disk_usage")
        self.assertEqual(risultato["status"], "tool_error")
        self.assertIn("disco non raggiungibile", risultato["error"])


# =====================================================================
# Pipeline post-tool generica applicata a un risultato "system" reale.
# =====================================================================

class TestPipelinePostToolSystem(unittest.TestCase):

    def setUp(self):
        self.risultato_tool = get_system_info({}, None)

    def test_secondo_giro_riuscito(self):
        stream = [_messaggio("Stai usando Windows.")]
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
                fallback_deterministico=fallback_deterministico_sistema,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "Stai usando Windows.")
        self.assertEqual(contatore.chiamate, 1)

    def test_secondo_giro_fallito_usa_fallback_sistema(self):
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
                fallback_deterministico=fallback_deterministico_sistema,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertIn(self.risultato_tool["data"]["os_name"], risposta)
        self.assertNotIn("memoria", risposta.lower())
        self.assertNotIn("ricordo", risposta.lower())


class TestPipelinePostToolDiskUsage(unittest.TestCase):

    def setUp(self):
        self.risultato_tool = get_disk_usage({}, None)

    def test_secondo_giro_riuscito(self):
        stream = [_messaggio("Hai parecchio spazio libero.")]
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
                fallback_deterministico=fallback_deterministico_sistema,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "Hai parecchio spazio libero.")
        self.assertEqual(contatore.chiamate, 1)

    def test_secondo_giro_fallito_usa_fallback_disk_usage(self):
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
                fallback_deterministico=fallback_deterministico_sistema,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertIn("Spazio totale", risposta)
        self.assertNotIn("memoria", risposta.lower())
        self.assertNotIn("ricordo", risposta.lower())
        self.assertNotIn("Sistema operativo", risposta)


if __name__ == "__main__":
    unittest.main()
