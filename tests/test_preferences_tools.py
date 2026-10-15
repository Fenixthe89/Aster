"""
0.7.4 - modules/preferences_tools.py: tool get_app_preferences e
set_app_preference, dominio "preferences".

Copre schema, registrazione nel RegistroStrumenti combinato (16 tool
totali: 7 memoria, 5 sistema, 2 preferences, 2 filesystem), gli
handler reali con backend app_preferences.py su directory temporanee
(nessun mock del backend: qui si verifica il contratto tool, il
backend è già coperto da tests/test_app_preferences.py), la
sanitizzazione di value (percorsi, comandi, caratteri Unicode
pericolosi, lunghezza), il contratto "value sempre obbligatorio" (un
parametro mancante non cancella nulla), il fallback deterministico, la
pipeline post-tool, l'assenza di percorsi/eccezioni nei risultati, e
la protezione del file ad opera della Reserved Data Root già esistente
(nessuna modifica a modules/filesystem_policy.py).

0.7.4 conferma Python: set_app_preference (l'handler REGISTRATO, quindi
raggiungibile anche da una chiamata diretta al registry) valida e
propone soltanto (status="pending_confirmation", ok=False): non scrive
mai, nemmeno quando il file è corrotto. L'unica funzione capace di
scrivere è applica_preferenza_confermata, non registrata come tool: la
copre la sezione dedicata più sotto, insieme al test di bypass che
dimostra che anche registro.dispatch(...) non può aggirare questo
confine. Il flusso completo (domanda sì/no via input(), nessun secondo
giro Ollama, cronologia senza tool call orfane) è testato
separatamente in tests/test_chat_preferences_confirm.py, perché vive
nel ciclo di modules/chat.py.

Solo directory temporanee: nessun file in data/ reale viene mai letto
o scritto da questi test.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.app_preferences as app_preferences
import modules.preferences_tools as preferences_tools
import modules.tool_response as tool_response
from modules.file_tools import registra_tool_filesystem
from modules.filesystem_policy import risolvi_path_autorizzato
from modules.preferences_tools import (
    TOOLS_PREFERENZE,
    ContestoPreferenze,
    applica_preferenza_confermata,
    domanda_conferma_set_app_preference,
    fallback_deterministico_preferenze,
    get_app_preferences,
    registra_tool_preferenze,
    risposta_affermativa,
    risultato_set_app_preference_annullato,
    set_app_preference,
)
from modules.system_tools import registra_tool_sistema
from modules.tool_registry import RegistroStrumenti, crea_registro_memoria
from modules.tool_response import genera_risposta_post_tool


def _schema_per_nome(nome: str) -> dict:
    for schema in TOOLS_PREFERENZE:
        if schema["function"]["name"] == nome:
            return schema
    raise AssertionError(f"Schema non trovato in TOOLS_PREFERENZE: {nome}")


class _BaseTmp(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aster_test_pref_tools_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.percorso = self.tmp / "app_preferences.json"
        self.contesto = ContestoPreferenze(percorso_preferenze=self.percorso)


# =====================================================================
# Schema
# =====================================================================

class TestSchemaGetAppPreferences(unittest.TestCase):

    def test_schema_valido(self):
        schema = _schema_per_nome("get_app_preferences")
        self.assertEqual(schema["type"], "function")
        self.assertIsInstance(schema["function"]["description"], str)
        self.assertTrue(schema["function"]["description"].strip())

    def test_nessun_parametro(self):
        parametri = _schema_per_nome("get_app_preferences")["function"]["parameters"]
        self.assertEqual(parametri.get("properties"), {})
        self.assertEqual(parametri.get("required"), [])


class TestSchemaSetAppPreference(unittest.TestCase):

    def setUp(self):
        self.schema = _schema_per_nome("set_app_preference")
        self.parametri = self.schema["function"]["parameters"]

    def test_schema_valido(self):
        self.assertEqual(self.schema["type"], "function")
        self.assertIsInstance(self.schema["function"]["description"], str)

    def test_category_enum_chiuso(self):
        self.assertEqual(
            set(self.parametri["properties"]["category"]["enum"]),
            {"browser", "editor", "media_player"},
        )

    def test_entrambi_i_parametri_obbligatori(self):
        self.assertEqual(set(self.parametri.get("required")), {"category", "value"})

    def test_descrizione_non_promette_esecuzione(self):
        descrizione = self.schema["function"]["description"]
        self.assertIn("non avvia", descrizione)
        self.assertIn("non cambia", descrizione)


# =====================================================================
# Registrazione
# =====================================================================

class TestRegistrazione(unittest.TestCase):

    def test_dominio_e_livelli(self):
        registro = crea_registro_memoria()
        registra_tool_preferenze(registro)

        get_spec = registro.trova("get_app_preferences")
        set_spec = registro.trova("set_app_preference")

        self.assertEqual(get_spec.dominio, "preferences")
        self.assertEqual(set_spec.dominio, "preferences")
        self.assertEqual(get_spec.livello, "READ_ONLY")
        self.assertEqual(set_spec.livello, "CONFIRM_REQUIRED")
        self.assertIs(get_spec.handler, get_app_preferences)
        self.assertIs(set_spec.handler, set_app_preference)

    def test_registro_completo_sedici_tool(self):
        registro = crea_registro_memoria()
        registra_tool_sistema(registro)
        registra_tool_preferenze(registro)
        registra_tool_filesystem(registro)

        nomi = {s["function"]["name"] for s in registro.elenco_schema()}
        self.assertEqual(len(nomi), 16)
        self.assertIn("get_app_preferences", nomi)
        self.assertIn("set_app_preference", nomi)

    def test_nessun_tool_esistente_alterato(self):
        registro = crea_registro_memoria()
        registra_tool_sistema(registro)
        nomi_prima = {s["function"]["name"] for s in registro.elenco_schema()}

        registra_tool_preferenze(registro)
        for nome in nomi_prima:
            self.assertIsNotNone(registro.trova(nome), msg=nome)


# =====================================================================
# get_app_preferences
# =====================================================================

class TestGetAppPreferences(_BaseTmp):

    def test_file_assente_tutte_null(self):
        esito = get_app_preferences({}, self.contesto)
        self.assertEqual(
            esito,
            {
                "ok": True,
                "operation": "get_app_preferences",
                "status": "success",
                "data": {"browser": None, "editor": None, "media_player": None},
            },
        )

    def test_dopo_applicazione_confermata_restituisce_il_valore(self):
        applica_preferenza_confermata("browser", "Firefox", self.contesto)
        esito = get_app_preferences({}, self.contesto)
        self.assertEqual(esito["data"]["browser"], "Firefox")

    def test_sola_proposta_non_visibile_in_get(self):
        """set_app_preference da solo non scrive: get_app_preferences non la vede."""

        set_app_preference({"category": "browser", "value": "Firefox"}, self.contesto)
        esito = get_app_preferences({}, self.contesto)
        self.assertIsNone(esito["data"]["browser"])

    def test_file_corrotto_stato_non_affidabile(self):
        self.percorso.write_text("{non json", encoding="utf-8")
        esito = get_app_preferences({}, self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "unreliable_state")

    def test_nessuna_eccezione_raw_su_corrotto(self):
        segreto = str(self.tmp / "Utenti" / "Segreto")
        self.percorso.write_text(f"{{corrotto {segreto}", encoding="utf-8")
        esito = get_app_preferences({}, self.contesto)
        serializzato = json.dumps(esito, ensure_ascii=False)
        self.assertNotIn("Segreto", serializzato)
        self.assertNotIn(str(self.tmp), serializzato)

    def test_argomenti_ignorati(self):
        esito = get_app_preferences({"qualunque": "cosa"}, self.contesto)
        self.assertTrue(esito["ok"])

    def test_valore_persistito_invalido_e_stato_non_affidabile(self):
        """FIX 2: anche un JSON strutturalmente valido ma con un'etichetta
        non valida (es. un percorso) è trattato come stato non affidabile
        da get_app_preferences, non restituito come se fosse una
        preferenza legittima."""

        dato = {
            "schema_version": 1,
            "preferences": {
                "browser": r"C:\Windows\System32\cmd.exe",
                "editor": None,
                "media_player": None,
            },
        }
        self.percorso.write_text(json.dumps(dato), encoding="utf-8")

        esito = get_app_preferences({}, self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "unreliable_state")

    def test_schema_version_booleano_e_stato_non_affidabile(self):
        dato = {
            "schema_version": True,
            "preferences": {"browser": None, "editor": None, "media_player": None},
        }
        self.percorso.write_text(json.dumps(dato), encoding="utf-8")

        esito = get_app_preferences({}, self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "unreliable_state")


# =====================================================================
# set_app_preference - contratto e validazione (0.7.4: propone soltanto,
# non scrive mai; vedi TestApplicaPreferenzaConfermata per la scrittura)
# =====================================================================

class TestSetAppPreferenceProposta(_BaseTmp):

    def test_proposta_valida_non_scrive(self):
        esito = set_app_preference(
            {"category": "editor", "value": "Visual Studio Code"}, self.contesto
        )
        self.assertEqual(
            esito,
            {
                "ok": False,
                "operation": "set_app_preference",
                "status": "pending_confirmation",
                "data": {"category": "editor", "value": "Visual Studio Code"},
            },
        )
        self.assertFalse(self.percorso.exists())

    def test_proposta_cancellazione_con_null_non_scrive(self):
        applica_preferenza_confermata("browser", "Firefox", self.contesto)
        esito = set_app_preference(
            {"category": "browser", "value": None}, self.contesto
        )
        self.assertEqual(esito["data"], {"category": "browser", "value": None})
        self.assertEqual(esito["status"], "pending_confirmation")
        # La preferenza precedente resta sul disco: la proposta di
        # cancellazione da sola non ha ancora effetto.
        self.assertEqual(
            get_app_preferences({}, self.contesto)["data"]["browser"], "Firefox"
        )

    def test_nomi_applicativi_normali_proposti(self):
        for nome in (
            "Firefox", "Visual Studio Code", "VLC media player",
            "Notepad++", "MPC-HC", "7-Zip",
        ):
            with self.subTest(nome=nome):
                esito = set_app_preference(
                    {"category": "browser", "value": nome}, self.contesto
                )
                self.assertEqual(esito["status"], "pending_confirmation", msg=nome)
                self.assertEqual(esito["data"]["value"], nome)

    def test_spazi_esterni_rimossi_nella_proposta(self):
        esito = set_app_preference(
            {"category": "browser", "value": "  Firefox  "}, self.contesto
        )
        self.assertEqual(esito["data"]["value"], "Firefox")

    def test_mai_fatta_alcuna_scrittura_anche_dopo_piu_proposte(self):
        for nome in ("Firefox", "Chrome", "Edge"):
            set_app_preference({"category": "browser", "value": nome}, self.contesto)
        self.assertFalse(self.percorso.exists())


class TestSetAppPreferenceValidazione(_BaseTmp):

    def test_categoria_mancante(self):
        esito = set_app_preference({"value": "Firefox"}, self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "validation_error")

    def test_categoria_non_valida(self):
        esito = set_app_preference(
            {"category": "terminale", "value": "x"}, self.contesto
        )
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "validation_error")

    def test_value_assente_e_rifiutato_non_e_cancellazione(self):
        esito = set_app_preference({"category": "browser"}, self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "validation_error")
        # Nessuna scrittura deve essere avvenuta.
        self.assertFalse(self.percorso.exists())

    def test_value_stringa_vuota_rifiutata(self):
        for valore in ("", "   "):
            with self.subTest(valore=repr(valore)):
                esito = set_app_preference(
                    {"category": "browser", "value": valore}, self.contesto
                )
                self.assertFalse(esito["ok"])
                self.assertEqual(esito["status"], "validation_error")

    def test_value_tipo_non_valido_rifiutato(self):
        for valore in (42, [], {}, True):
            with self.subTest(valore=valore):
                esito = set_app_preference(
                    {"category": "browser", "value": valore}, self.contesto
                )
                self.assertFalse(esito["ok"])

    def test_value_troppo_lungo_rifiutato(self):
        esito = set_app_preference(
            {"category": "browser", "value": "A" * 200}, self.contesto
        )
        self.assertFalse(esito["ok"])

    def test_value_percorso_rifiutato(self):
        percorsi_vietati = (
            r"C:\Windows\System32\cmd.exe",
            r"C:\Program Files\App\app.exe",
            "/usr/bin/firefox",
            r".\relativo\path.exe",
        )
        for valore in percorsi_vietati:
            with self.subTest(valore=valore):
                esito = set_app_preference(
                    {"category": "browser", "value": valore}, self.contesto
                )
                self.assertFalse(esito["ok"], msg=valore)
                self.assertEqual(esito["status"], "validation_error")

    def test_value_comando_rifiutato(self):
        comandi_vietati = (
            "firefox; rm -rf /",
            "firefox && calc",
            "firefox | more",
            "firefox > out.txt",
            "`whoami`",
            "$(whoami)",
            "--help",
        )
        for valore in comandi_vietati:
            with self.subTest(valore=valore):
                esito = set_app_preference(
                    {"category": "browser", "value": valore}, self.contesto
                )
                self.assertFalse(esito["ok"], msg=valore)

    def test_value_estensione_eseguibile_rifiutata(self):
        for valore in ("app.exe", "script.bat", "setup.msi", "run.ps1"):
            with self.subTest(valore=valore):
                esito = set_app_preference(
                    {"category": "browser", "value": valore}, self.contesto
                )
                self.assertFalse(esito["ok"], msg=valore)

    def test_value_lettera_unita_rifiutata(self):
        esito = set_app_preference(
            {"category": "browser", "value": "C:something"}, self.contesto
        )
        self.assertFalse(esito["ok"])

    def test_value_carattere_controllo_rifiutato(self):
        esito = set_app_preference(
            {"category": "browser", "value": "Fire\x07fox"}, self.contesto
        )
        self.assertFalse(esito["ok"])

    def test_value_surrogato_isolato_rifiutato(self):
        esito = set_app_preference(
            {"category": "browser", "value": "Fire" + chr(0xD800) + "fox"},
            self.contesto,
        )
        self.assertFalse(esito["ok"])

    def test_value_marcatore_formattazione_invisibile_rifiutato(self):
        esito = set_app_preference(
            {"category": "browser", "value": "Fire" + chr(0x202E) + "fox"},
            self.contesto,
        )
        self.assertFalse(esito["ok"])

    def test_argomenti_non_dict_non_falliscono(self):
        esito = set_app_preference(None, self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "validation_error")

    def test_nessuna_scrittura_su_validazione_fallita(self):
        set_app_preference({"category": "browser", "value": "C:\\cmd.exe"}, self.contesto)
        self.assertFalse(self.percorso.exists())


class TestPropostaIgnoraLoStatoDelFile(_BaseTmp):
    """La proposta non legge nemmeno il file: non si accorge di un file corrotto."""

    def test_proposta_valida_anche_con_file_corrotto(self):
        self.percorso.write_text("{non json", encoding="utf-8")
        esito = set_app_preference(
            {"category": "browser", "value": "Firefox"}, self.contesto
        )
        self.assertEqual(esito["status"], "pending_confirmation")
        # Il file corrotto non viene mai toccato dalla sola proposta.
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), "{non json")


# =====================================================================
# applica_preferenza_confermata - l'unica funzione che scrive (0.7.4)
# =====================================================================

class TestApplicaPreferenzaConfermata(_BaseTmp):

    def test_scrive_su_disco(self):
        esito = applica_preferenza_confermata("browser", "Firefox", self.contesto)
        self.assertEqual(
            esito,
            {
                "ok": True,
                "operation": "set_app_preference",
                "status": "success",
                "data": {"category": "browser", "value": "Firefox"},
            },
        )
        self.assertTrue(self.percorso.exists())
        self.assertEqual(
            get_app_preferences({}, self.contesto)["data"]["browser"], "Firefox"
        )

    def test_cancellazione_con_none(self):
        applica_preferenza_confermata("browser", "Firefox", self.contesto)
        esito = applica_preferenza_confermata("browser", None, self.contesto)
        self.assertEqual(esito["data"], {"category": "browser", "value": None})
        self.assertIsNone(
            get_app_preferences({}, self.contesto)["data"]["browser"]
        )

    def test_rivalida_categoria(self):
        esito = applica_preferenza_confermata("terminale", "x", self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "validation_error")
        self.assertFalse(self.percorso.exists())

    def test_rivalida_valore(self):
        esito = applica_preferenza_confermata(
            "browser", r"C:\Windows\System32\cmd.exe", self.contesto
        )
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "validation_error")
        self.assertFalse(self.percorso.exists())

    def test_file_corrotto_blocca_scrittura(self):
        self.percorso.write_text("{non json", encoding="utf-8")
        esito = applica_preferenza_confermata("browser", "Firefox", self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "unreliable_state")
        # Il file corrotto non viene mai sovrascritto: errore di
        # persistenza dopo conferma, nessuna scrittura avvenuta.
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), "{non json")

    def test_valore_persistito_invalido_blocca_scrittura(self):
        """FIX 2: un file JSON valido ma con un'etichetta non valida già
        persistita (es. un percorso) blocca comunque la scrittura, come
        un file corrotto: non si sovrascrive mai uno stato non
        affidabile, qualunque sia la ragione per cui non lo è."""

        contenuto_prima = json.dumps({
            "schema_version": 1,
            "preferences": {
                "browser": r"C:\Windows\System32\cmd.exe",
                "editor": None,
                "media_player": None,
            },
        })
        self.percorso.write_text(contenuto_prima, encoding="utf-8")

        esito = applica_preferenza_confermata("editor", "Visual Studio Code", self.contesto)

        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "unreliable_state")
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), contenuto_prima)

    def test_nessuna_eccezione_raw(self):
        segreto = str(self.tmp / "Utenti" / "Segreto")
        self.percorso.write_text(f"{{corrotto {segreto}", encoding="utf-8")
        esito = applica_preferenza_confermata("browser", "Firefox", self.contesto)
        serializzato = json.dumps(esito, ensure_ascii=False)
        self.assertNotIn("Segreto", serializzato)
        self.assertNotIn(str(self.tmp), serializzato)

    def test_errore_imprevisto_tool_error(self):
        with mock.patch.object(
            preferences_tools.app_preferences, "imposta_preferenza",
            side_effect=RuntimeError("boom"),
        ):
            esito = applica_preferenza_confermata("browser", "Firefox", self.contesto)
        self.assertFalse(esito["ok"])
        self.assertEqual(esito["status"], "tool_error")
        self.assertNotIn("boom", json.dumps(esito))


# =====================================================================
# Impossibilità di bypass: nemmeno una chiamata diretta al registry
# può raggiungere una scrittura (0.7.4)
# =====================================================================

class TestBypassImpossibile(_BaseTmp):

    def test_dispatch_diretto_sul_registry_non_scrive(self):
        registro = RegistroStrumenti()
        registra_tool_preferenze(registro)

        esito = registro.dispatch(
            "set_app_preference",
            {"category": "browser", "value": "Firefox"},
            self.contesto,
        )

        self.assertEqual(esito["status"], "pending_confirmation")
        self.assertFalse(self.percorso.exists())

    def test_handler_registrato_non_importa_mai_imposta_preferenza(self):
        """set_app_preference non può scrivere per costruzione: non chiama mai
        app_preferences.imposta_preferenza, nemmeno se lo si rende esplosivo."""

        with mock.patch.object(
            preferences_tools.app_preferences, "imposta_preferenza",
            side_effect=AssertionError("set_app_preference non deve mai scrivere"),
        ):
            for valore in ("Firefox", None, "Chrome"):
                set_app_preference(
                    {"category": "browser", "value": valore}, self.contesto
                )

    def test_molte_chiamate_dispatch_ripetute_non_scrivono_mai(self):
        registro = RegistroStrumenti()
        registra_tool_preferenze(registro)

        for _ in range(5):
            registro.dispatch(
                "set_app_preference",
                {"category": "media_player", "value": "VLC"},
                self.contesto,
            )

        self.assertFalse(self.percorso.exists())


# =====================================================================
# risposta_affermativa - normalizzazione della conferma (0.7.4)
# =====================================================================

class TestRispostaAffermativa(unittest.TestCase):

    def test_risposte_affermative_esplicite(self):
        for testo in ("si", "Si", "SI", "sì", "Sì", "yes", "Yes", "YES",
                      "  si  ", "\tsì\n"):
            with self.subTest(testo=repr(testo)):
                self.assertTrue(risposta_affermativa(testo), msg=repr(testo))

    def test_risposte_negative(self):
        for testo in ("no", "No", "NO", "  no "):
            with self.subTest(testo=repr(testo)):
                self.assertFalse(risposta_affermativa(testo))

    def test_risposte_ambigue_non_confermano(self):
        for testo in (
            "forse", "si però aspetta", "sicuro", "sissignore",
            "yesss", "ok", "va bene", "", "   ", "si no",
        ):
            with self.subTest(testo=repr(testo)):
                self.assertFalse(risposta_affermativa(testo), msg=repr(testo))

    def test_nessuna_conferma_per_sottostringa(self):
        # "si" è contenuto in "così" e "necessita", ma non deve bastare.
        for testo in ("così", "necessità di conferma", "siamo d'accordo"):
            with self.subTest(testo=testo):
                self.assertFalse(risposta_affermativa(testo))

    def test_tipi_non_stringa_non_confermano(self):
        for valore in (None, 1, True, [], {}):
            with self.subTest(valore=valore):
                self.assertFalse(risposta_affermativa(valore))


class TestDomandaConfermaSetAppPreference(unittest.TestCase):

    def test_domanda_per_impostazione_menziona_il_valore(self):
        domanda = domanda_conferma_set_app_preference("browser", "Firefox")
        self.assertIn("Firefox", domanda)
        self.assertIn("browser", domanda)

    def test_domanda_per_cancellazione_non_menziona_un_valore(self):
        domanda = domanda_conferma_set_app_preference("editor", None)
        self.assertIn("cancellare", domanda.lower())
        self.assertIn("editor", domanda.lower())

    def test_categoria_sconosciuta_non_rompe_la_domanda(self):
        domanda = domanda_conferma_set_app_preference("qualcosa", "x")
        self.assertIsInstance(domanda, str)
        self.assertTrue(domanda.strip())


# =====================================================================
# Fallback deterministico
# =====================================================================

class TestFallbackGetAppPreferences(unittest.TestCase):

    def test_tutte_impostate(self):
        esito = {
            "ok": True, "operation": "get_app_preferences", "status": "success",
            "data": {"browser": "Firefox", "editor": "VS Code", "media_player": "VLC"},
        }
        testo = fallback_deterministico_preferenze(esito)
        self.assertIn("Firefox", testo)
        self.assertIn("VS Code", testo)
        self.assertIn("VLC", testo)

    def test_nessuna_impostata(self):
        esito = {
            "ok": True, "operation": "get_app_preferences", "status": "success",
            "data": {"browser": None, "editor": None, "media_player": None},
        }
        testo = fallback_deterministico_preferenze(esito)
        self.assertIn("nessuna preferenza salvata", testo.lower())

    def test_stato_non_affidabile(self):
        esito = {
            "ok": False, "operation": "get_app_preferences",
            "status": "unreliable_state", "error": "non leggibili",
        }
        self.assertEqual(fallback_deterministico_preferenze(esito), "non leggibili")

    def test_dati_malformati_mai_crash(self):
        for data in (None, "x", 42, []):
            with self.subTest(data=data):
                esito = {
                    "ok": True, "operation": "get_app_preferences",
                    "status": "success", "data": data,
                }
                testo = fallback_deterministico_preferenze(esito)
                self.assertIsInstance(testo, str)


class TestFallbackSetAppPreference(unittest.TestCase):

    def test_impostata(self):
        esito = {
            "ok": True, "operation": "set_app_preference", "status": "success",
            "data": {"category": "browser", "value": "Firefox"},
        }
        testo = fallback_deterministico_preferenze(esito)
        self.assertIn("Firefox", testo)

    def test_cancellata(self):
        esito = {
            "ok": True, "operation": "set_app_preference", "status": "success",
            "data": {"category": "browser", "value": None},
        }
        testo = fallback_deterministico_preferenze(esito)
        self.assertIn("cancellat", testo.lower())

    def test_errore_validazione(self):
        esito = {
            "ok": False, "operation": "set_app_preference",
            "status": "validation_error", "error": "value non valido: x",
        }
        self.assertEqual(fallback_deterministico_preferenze(esito), "value non valido: x")

    def test_pending_confirmation_mostra_la_domanda_non_un_successo(self):
        esito = set_app_preference(
            {"category": "browser", "value": "Firefox"}, ContestoPreferenze(Path("x"))
        )
        testo = fallback_deterministico_preferenze(esito)
        self.assertIn("Confermi", testo)
        self.assertNotIn("salvata", testo.lower())

    def test_cancelled_non_e_un_errore_ne_un_successo_di_scrittura(self):
        esito = risultato_set_app_preference_annullato("browser", "Firefox")
        testo = fallback_deterministico_preferenze(esito)
        self.assertIn("annullat", testo.lower())
        self.assertNotIn("Preferenza salvata", testo)

    def test_router_non_usa_fallback_generico(self):
        esito = {
            "ok": True, "operation": "set_app_preference", "status": "success",
            "data": {"category": "browser", "value": "Firefox"},
        }
        testo = fallback_deterministico_preferenze(esito)
        self.assertNotIn("Non sono riuscito a generare", testo)


# =====================================================================
# Pipeline post-tool
# =====================================================================

class TestPipelinePostToolPreferenze(_BaseTmp):
    """
    get_app_preferences passa ancora per la pipeline generica (nessuna
    conferma necessaria, è sola lettura). set_app_preference invece,
    nel ciclo reale, non ci arriva mai: modules/chat.py intercetta
    status="pending_confirmation" prima di questa pipeline (vedi
    tests/test_chat_preferences_confirm.py). Qui si verifica solo che
    il fallback resti coerente se qualcosa chiamasse comunque la
    pipeline generica su un risultato di set_app_preference.
    """

    def test_secondo_giro_fallito_usa_fallback_get(self):
        applica_preferenza_confermata("browser", "Firefox", self.contesto)
        risultato_tool = get_app_preferences({}, self.contesto)

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
                risultato_tool=risultato_tool,
                salta_secondo_giro=False,
                fallback_deterministico=fallback_deterministico_preferenze,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertIn("Firefox", risposta)

    def test_set_app_preference_pending_mai_presentato_come_riuscito(self):
        """Anche se qualcosa chiamasse la pipeline generica su una proposta
        (non dovrebbe accadere nel ciclo reale), il fallback non direbbe mai
        che la preferenza è stata salvata."""

        risultato_tool = set_app_preference(
            {"category": "browser", "value": "Firefox"}, self.contesto
        )
        testo = fallback_deterministico_preferenze(risultato_tool)
        self.assertNotIn("salvata", testo.lower())


# =====================================================================
# Reserved Data Root (nessuna modifica a filesystem_policy.py: si
# verifica solo che il meccanismo esistente copra anche questo file)
# =====================================================================

class TestReservedDataRoot(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="aster_test_reserved_prefs_"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_root = self.base / "app"
        self.data_root = self.app_root / "data"
        self.data_root.mkdir(parents=True)
        (self.data_root / "app_preferences.json").write_text("{}", encoding="utf-8")

    def test_app_preferences_negato_anche_con_app_root_autorizzata(self):
        decisione = risolvi_path_autorizzato(
            self.data_root / "app_preferences.json",
            allowed_roots=[self.app_root],
            reserved_roots=(self.data_root,),
        )
        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)

    def test_app_preferences_negato_percorso_relativo(self):
        decisione = risolvi_path_autorizzato(
            "data/app_preferences.json",
            allowed_roots=[self.app_root],
            reserved_roots=(self.data_root,),
        )
        self.assertFalse(decisione.allowed)

    def test_sibling_nella_stessa_data_root_resta_coperto(self):
        (self.data_root / "memory.json").write_text("{}", encoding="utf-8")
        decisione = risolvi_path_autorizzato(
            self.data_root / "memory.json",
            allowed_roots=[self.app_root],
            reserved_roots=(self.data_root,),
        )
        self.assertFalse(decisione.allowed)

    def test_senza_reserved_la_stessa_root_sarebbe_leggibile(self):
        """Controllo di validita' del test: conferma che il deny sopra viene
        dalla reserved root, non da qualche altro limite del path."""

        decisione = risolvi_path_autorizzato(
            self.data_root / "app_preferences.json",
            allowed_roots=[self.app_root],
            reserved_roots=(),
        )
        self.assertTrue(decisione.allowed)


if __name__ == "__main__":
    unittest.main()
