"""
0.6.3 - get_system_info: test di regressione.

Verifica il primo tool non-memory reale di Aster: schema, registrazione
nel RegistroStrumenti (dominio "system", livello READ_ONLY) accanto ai
7 tool memoria esistenti, handler reale (nessun mock del sistema
operativo: legge i valori veri della macchina che esegue i test),
assenza di dati personali, e il fallback deterministico del dominio.

Copre anche la pipeline post-tool generica (modules/tool_response.py,
invariata) applicata a un risultato "system" reale.

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
    get_system_info,
    registra_tool_sistema,
)
from modules.tool_registry import RegistroStrumenti, crea_registro_memoria
from modules.tool_response import genera_risposta_post_tool

CAMPI_ATTESI = {
    "os_name",
    "os_release",
    "machine",
    "python_version",
    "cpu_count",
    "processor",
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
}


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

class TestSchemaGetSystemInfo(unittest.TestCase):

    def test_schema_valido(self):
        self.assertEqual(len(TOOLS_SISTEMA), 1)
        schema = TOOLS_SISTEMA[0]

        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "get_system_info")
        self.assertIsInstance(schema["function"]["description"], str)
        self.assertTrue(schema["function"]["description"].strip())

    def test_nessun_parametro_richiesto(self):
        parametri = TOOLS_SISTEMA[0]["function"]["parameters"]

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
        tool_spec = self.registro.trova("get_system_info")
        self.assertIsNotNone(tool_spec)
        self.assertEqual(tool_spec.dominio, "system")

    def test_livello_read_only(self):
        tool_spec = self.registro.trova("get_system_info")
        self.assertEqual(tool_spec.livello, "READ_ONLY")

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
        self.assertEqual(len(nomi), 8)

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
        self.assertEqual(set(data.keys()), CAMPI_ATTESI)

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


if __name__ == "__main__":
    unittest.main()
