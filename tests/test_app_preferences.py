"""
0.7.4 - modules/app_preferences.py: persistenza delle preferenze
applicative strutturate (browser, editor, media_player).

Copre lo schema v1 (categorie chiuse, valori stringa o null), il primo
avvio senza file, la scrittura atomica (file temporaneo, validazione,
os.replace), la preservazione del file precedente quando la
sostituzione fallisce, il file corrotto o con schema incompatibile
trattato come stato non affidabile (mai sostituito silenziosamente con
preferenze vuote) e il blocco degli aggiornamenti in quel caso.

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
from modules.app_preferences import (
    CATEGORIE_VALIDE,
    ErrorePreferenzeNonAffidabili,
    SCHEMA_VERSION,
    valore_preferenza_valido,
)


class _BaseTmp(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aster_test_app_prefs_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.percorso = self.tmp / "app_preferences.json"


class TestStrutturaVuota(_BaseTmp):

    def test_crea_preferenze_vuote(self):
        self.assertEqual(
            app_preferences.crea_preferenze_vuote(),
            {
                "schema_version": SCHEMA_VERSION,
                "preferences": {"browser": None, "editor": None, "media_player": None},
            },
        )

    def test_categorie_valide(self):
        self.assertEqual(set(CATEGORIE_VALIDE), {"browser", "editor", "media_player"})


class TestValidazioneSchema(unittest.TestCase):

    def test_schema_valido_non_solleva(self):
        app_preferences.valida_preferenze(app_preferences.crea_preferenze_vuote())

    def test_non_dict_solleva_typeerror(self):
        for valore in (None, "x", 42, [], True):
            with self.subTest(valore=valore):
                with self.assertRaises(TypeError):
                    app_preferences.valida_preferenze(valore)

    def test_chiave_mancante_solleva(self):
        with self.assertRaises(ValueError):
            app_preferences.valida_preferenze({"schema_version": 1})

    def test_chiave_aggiuntiva_solleva(self):
        dato = app_preferences.crea_preferenze_vuote()
        dato["extra"] = "x"
        with self.assertRaises(ValueError):
            app_preferences.valida_preferenze(dato)

    def test_versione_schema_non_supportata_solleva(self):
        dato = app_preferences.crea_preferenze_vuote()
        dato["schema_version"] = 2
        with self.assertRaises(ValueError):
            app_preferences.valida_preferenze(dato)

    def test_versione_schema_booleana_solleva(self):
        """FIX 2: True == 1 in Python; un confronto debole accetterebbe un bool."""

        for valore in (True, False):
            with self.subTest(valore=valore):
                dato = app_preferences.crea_preferenze_vuote()
                dato["schema_version"] = valore
                with self.assertRaises(ValueError):
                    app_preferences.valida_preferenze(dato)

    def test_versione_schema_float_solleva(self):
        dato = app_preferences.crea_preferenze_vuote()
        dato["schema_version"] = 1.0
        with self.assertRaises(ValueError):
            app_preferences.valida_preferenze(dato)

    def test_preferences_non_dict_solleva(self):
        dato = app_preferences.crea_preferenze_vuote()
        dato["preferences"] = "x"
        with self.assertRaises(TypeError):
            app_preferences.valida_preferenze(dato)

    def test_categoria_mancante_solleva(self):
        dato = app_preferences.crea_preferenze_vuote()
        del dato["preferences"]["editor"]
        with self.assertRaises(ValueError):
            app_preferences.valida_preferenze(dato)

    def test_categoria_aggiuntiva_solleva(self):
        dato = app_preferences.crea_preferenze_vuote()
        dato["preferences"]["terminale"] = None
        with self.assertRaises(ValueError):
            app_preferences.valida_preferenze(dato)

    def test_valore_non_stringa_ne_none_solleva(self):
        for valore in (42, [], {}, True):
            with self.subTest(valore=valore):
                dato = app_preferences.crea_preferenze_vuote()
                dato["preferences"]["browser"] = valore
                with self.assertRaises(TypeError):
                    app_preferences.valida_preferenze(dato)

    def test_valore_stringa_valido(self):
        dato = app_preferences.crea_preferenze_vuote()
        dato["preferences"]["browser"] = "Firefox"
        app_preferences.valida_preferenze(dato)

    def test_valori_persistenti_invalidi_sollevano_valueerror(self):
        """FIX 2: stesse regole di set_app_preference, applicate anche ai
        valori già persistiti su disco (centralizzate in
        valore_preferenza_valido, non più duplicate)."""

        valori_invalidi = (
            r"C:\Windows\System32\cmd.exe",
            "/usr/bin/firefox",
            "firefox; rm -rf /",
            "firefox && calc",
            "`whoami`",
            "--help",
            "app.exe",
            "C:something",
            "Fire\x07fox",
            "Fire" + chr(0xD800) + "fox",
            "Fire" + chr(0x202E) + "fox",
            "",
            "   ",
            "A" * 200,
        )
        for valore in valori_invalidi:
            with self.subTest(valore=repr(valore)):
                dato = app_preferences.crea_preferenze_vuote()
                dato["preferences"]["browser"] = valore
                with self.assertRaises(ValueError):
                    app_preferences.valida_preferenze(dato)

    def test_etichette_legittime_preesistenti_restano_valide(self):
        """Compatibilità: nomi applicativi reali non devono mai essere
        rifiutati dalla centralizzazione del FIX 2."""

        for nome in ("Firefox", "Visual Studio Code", "Notepad++", "MPC-HC",
                     "VLC media player", "7-Zip"):
            with self.subTest(nome=nome):
                dato = app_preferences.crea_preferenze_vuote()
                dato["preferences"]["browser"] = nome
                app_preferences.valida_preferenze(dato)  # non deve sollevare
                self.assertTrue(valore_preferenza_valido(nome))

    def test_valore_preferenza_valido_esposta_e_coerente_con_valida_preferenze(self):
        """La stessa funzione pura usata da valida_preferenze è pubblica e
        riusabile da altri moduli (centralizzazione, non due copie)."""

        self.assertTrue(valore_preferenza_valido("Firefox"))
        self.assertFalse(valore_preferenza_valido(r"C:\cmd.exe"))
        self.assertFalse(valore_preferenza_valido(123))
        self.assertFalse(valore_preferenza_valido(None))


class TestCaricaESalva(_BaseTmp):

    def test_file_assente_solleva_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            app_preferences.carica_preferenze(self.percorso)

    def test_json_corrotto_solleva_jsondecodeerror(self):
        self.percorso.write_text("{non json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            app_preferences.carica_preferenze(self.percorso)

    def test_schema_incompatibile_solleva(self):
        self.percorso.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            app_preferences.carica_preferenze(self.percorso)

    def test_salva_poi_carica_round_trip(self):
        preferenze = app_preferences.crea_preferenze_vuote()
        preferenze["preferences"]["browser"] = "Firefox"
        app_preferences.salva_preferenze(self.percorso, preferenze)

        ricaricato = app_preferences.carica_preferenze(self.percorso)
        self.assertEqual(ricaricato, preferenze)

    def test_salva_crea_la_directory_padre(self):
        percorso_annidato = self.tmp / "sotto" / "cartella" / "app_preferences.json"
        app_preferences.salva_preferenze(
            percorso_annidato, app_preferences.crea_preferenze_vuote()
        )
        self.assertTrue(percorso_annidato.exists())

    def test_salva_dati_non_validi_solleva_senza_scrivere(self):
        with self.assertRaises((ValueError, TypeError)):
            app_preferences.salva_preferenze(self.percorso, {"foo": "bar"})
        self.assertFalse(self.percorso.exists())

    def test_nessun_file_temporaneo_residuo_dopo_successo(self):
        app_preferences.salva_preferenze(
            self.percorso, app_preferences.crea_preferenze_vuote()
        )
        self.assertFalse(self.percorso.with_suffix(".tmp").exists())

    def test_scrittura_interrotta_preserva_il_file_precedente(self):
        originale = app_preferences.crea_preferenze_vuote()
        originale["preferences"]["browser"] = "Firefox"
        app_preferences.salva_preferenze(self.percorso, originale)
        contenuto_prima = self.percorso.read_text(encoding="utf-8")

        nuovo = app_preferences.crea_preferenze_vuote()
        nuovo["preferences"]["browser"] = "Chrome"

        with mock.patch.object(
            app_preferences.os, "replace", side_effect=OSError("disco pieno")
        ):
            with self.assertRaises(OSError):
                app_preferences.salva_preferenze(self.percorso, nuovo)

        # Il file principale non e' mai stato toccato: os.replace e'
        # l'unico punto che lo sostituisce, ed e' stato fatto fallire
        # prima di qualunque effetto.
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), contenuto_prima)
        self.assertFalse(self.percorso.with_suffix(".tmp").exists())

    def test_tmp_non_valido_non_tocca_il_principale(self):
        """Se la ricarica di validazione del tmp fallisse, os.replace non deve
        mai essere chiamato e il file principale resta quello precedente."""

        originale = app_preferences.crea_preferenze_vuote()
        app_preferences.salva_preferenze(self.percorso, originale)
        contenuto_prima = self.percorso.read_text(encoding="utf-8")

        nuovo = app_preferences.crea_preferenze_vuote()
        nuovo["preferences"]["browser"] = "Chrome"

        chiamate_replace = []
        with mock.patch.object(
            app_preferences, "carica_preferenze",
            side_effect=ValueError("tmp non valido"),
        ), mock.patch.object(
            app_preferences.os, "replace",
            side_effect=lambda *a: chiamate_replace.append(a),
        ):
            with self.assertRaises(ValueError):
                app_preferences.salva_preferenze(self.percorso, nuovo)

        self.assertEqual(chiamate_replace, [])
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), contenuto_prima)
        self.assertFalse(self.percorso.with_suffix(".tmp").exists())

    def test_errore_durante_la_scrittura_iniziale_pulisce_il_tmp(self):
        """0.7.4: json.dump/write ora sono dentro lo stesso try/finally di
        validazione e sostituzione: un errore qui non deve lasciare un
        .tmp parziale, e il file principale (se esiste) resta intatto."""

        originale = app_preferences.crea_preferenze_vuote()
        originale["preferences"]["browser"] = "Firefox"
        app_preferences.salva_preferenze(self.percorso, originale)
        contenuto_prima = self.percorso.read_text(encoding="utf-8")

        nuovo = app_preferences.crea_preferenze_vuote()
        nuovo["preferences"]["browser"] = "Chrome"

        with mock.patch.object(
            app_preferences.json, "dump", side_effect=OSError("errore di scrittura")
        ):
            with self.assertRaises(OSError):
                app_preferences.salva_preferenze(self.percorso, nuovo)

        self.assertEqual(self.percorso.read_text(encoding="utf-8"), contenuto_prima)
        self.assertFalse(self.percorso.with_suffix(".tmp").exists())

    def test_errore_durante_la_scrittura_iniziale_senza_file_precedente(self):
        """Stesso caso ma alla primissima scrittura (nessun file principale
        ancora esistente): nessun file, né principale né .tmp, deve restare."""

        with mock.patch.object(
            app_preferences.json, "dump", side_effect=OSError("errore di scrittura")
        ):
            with self.assertRaises(OSError):
                app_preferences.salva_preferenze(
                    self.percorso, app_preferences.crea_preferenze_vuote()
                )

        self.assertFalse(self.percorso.exists())
        self.assertFalse(self.percorso.with_suffix(".tmp").exists())


class TestLeggiOVuote(_BaseTmp):

    def test_file_assente_restituisce_vuote(self):
        self.assertEqual(
            app_preferences.leggi_preferenze_o_vuote(self.percorso),
            app_preferences.crea_preferenze_vuote(),
        )

    def test_file_valido_restituisce_contenuto(self):
        preferenze = app_preferences.crea_preferenze_vuote()
        preferenze["preferences"]["editor"] = "Visual Studio Code"
        app_preferences.salva_preferenze(self.percorso, preferenze)

        self.assertEqual(
            app_preferences.leggi_preferenze_o_vuote(self.percorso), preferenze
        )

    def test_json_corrotto_solleva_non_affidabili(self):
        self.percorso.write_text("{non json", encoding="utf-8")
        with self.assertRaises(ErrorePreferenzeNonAffidabili):
            app_preferences.leggi_preferenze_o_vuote(self.percorso)

    def test_schema_incompatibile_solleva_non_affidabili(self):
        self.percorso.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        with self.assertRaises(ErrorePreferenzeNonAffidabili):
            app_preferences.leggi_preferenze_o_vuote(self.percorso)

    def test_corrotto_non_viene_mai_sostituito_con_vuote(self):
        """Il file corrotto non deve mai sparire/essere riscritto da una sola lettura."""

        self.percorso.write_text("{non json", encoding="utf-8")
        with self.assertRaises(ErrorePreferenzeNonAffidabili):
            app_preferences.leggi_preferenze_o_vuote(self.percorso)
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), "{non json")

    def test_valore_persistito_invalido_solleva_non_affidabili(self):
        """FIX 2: un JSON strutturalmente valido ma con un'etichetta non
        valida (es. un percorso, mai scrivibile dal flusso normale ma
        ipotizzabile da una modifica manuale o una versione precedente)
        è comunque trattato come stato non affidabile, non silenziosamente
        accettato."""

        dato = {
            "schema_version": 1,
            "preferences": {
                "browser": r"C:\Windows\System32\cmd.exe",
                "editor": None,
                "media_player": None,
            },
        }
        self.percorso.write_text(json.dumps(dato), encoding="utf-8")

        with self.assertRaises(ErrorePreferenzeNonAffidabili):
            app_preferences.leggi_preferenze_o_vuote(self.percorso)

    def test_file_assente_non_dipende_da_path_exists(self):
        """FIX 1: nessuna chiamata separata a Path.exists(): anche se
        exists() mentisse (restituendo True su un file che in realtà
        non esiste), il comportamento dipende solo dall'apertura reale."""

        with mock.patch.object(Path, "exists", return_value=True):
            self.assertEqual(
                app_preferences.leggi_preferenze_o_vuote(self.percorso),
                app_preferences.crea_preferenze_vuote(),
            )

    def test_errore_di_lettura_simulato_e_non_affidabile_non_assente(self):
        """FIX 1: un errore di I/O diverso da file assente (es. permesso
        negato) deve restituire ErrorePreferenzeNonAffidabili, mai essere
        confuso con "nessuna preferenza impostata"."""

        self.percorso.write_text(
            json.dumps(app_preferences.crea_preferenze_vuote()), encoding="utf-8"
        )

        with mock.patch.object(
            Path, "open", side_effect=PermissionError("Accesso negato")
        ):
            with self.assertRaises(ErrorePreferenzeNonAffidabili):
                app_preferences.leggi_preferenze_o_vuote(self.percorso)

    def test_directory_al_posto_del_file_e_non_affidabile(self):
        """Un altro modo concreto di avere un errore di lettura reale
        (non simulato): IsADirectoryError, non FileNotFoundError."""

        cartella = self.tmp / "app_preferences_e_una_cartella.json"
        cartella.mkdir()
        with self.assertRaises(ErrorePreferenzeNonAffidabili):
            app_preferences.leggi_preferenze_o_vuote(cartella)

    def test_valori_validi_preesistenti_restano_invariati(self):
        """Un round-trip su etichette legittime non le altera in alcun modo."""

        preferenze = app_preferences.crea_preferenze_vuote()
        preferenze["preferences"]["browser"] = "Firefox"
        preferenze["preferences"]["editor"] = "Visual Studio Code"
        preferenze["preferences"]["media_player"] = "VLC media player"
        app_preferences.salva_preferenze(self.percorso, preferenze)

        riletto = app_preferences.leggi_preferenze_o_vuote(self.percorso)
        self.assertEqual(riletto, preferenze)


class TestImpostaPreferenza(_BaseTmp):

    def test_prima_scrittura_crea_il_file(self):
        self.assertFalse(self.percorso.exists())
        app_preferences.imposta_preferenza(self.percorso, "browser", "Firefox")
        self.assertTrue(self.percorso.exists())

    def test_imposta_poi_get_coerente(self):
        app_preferences.imposta_preferenza(self.percorso, "browser", "Firefox")
        preferenze = app_preferences.leggi_preferenze_o_vuote(self.percorso)
        self.assertEqual(preferenze["preferences"]["browser"], "Firefox")

    def test_aggiornamento_non_altera_le_altre_categorie(self):
        app_preferences.imposta_preferenza(self.percorso, "browser", "Firefox")
        app_preferences.imposta_preferenza(self.percorso, "editor", "VS Code")

        preferenze = app_preferences.leggi_preferenze_o_vuote(self.percorso)
        self.assertEqual(preferenze["preferences"]["browser"], "Firefox")
        self.assertEqual(preferenze["preferences"]["editor"], "VS Code")
        self.assertIsNone(preferenze["preferences"]["media_player"])

    def test_sostituzione_di_una_categoria_gia_impostata(self):
        app_preferences.imposta_preferenza(self.percorso, "browser", "Firefox")
        app_preferences.imposta_preferenza(self.percorso, "browser", "Chrome")

        preferenze = app_preferences.leggi_preferenze_o_vuote(self.percorso)
        self.assertEqual(preferenze["preferences"]["browser"], "Chrome")

    def test_cancellazione_esplicita_con_none(self):
        app_preferences.imposta_preferenza(self.percorso, "browser", "Firefox")
        app_preferences.imposta_preferenza(self.percorso, "browser", None)

        preferenze = app_preferences.leggi_preferenze_o_vuote(self.percorso)
        self.assertIsNone(preferenze["preferences"]["browser"])

    def test_categoria_non_valida_solleva(self):
        with self.assertRaises(ValueError):
            app_preferences.imposta_preferenza(self.percorso, "terminale", "x")

    def test_valore_tipo_non_valido_solleva(self):
        with self.assertRaises(TypeError):
            app_preferences.imposta_preferenza(self.percorso, "browser", 42)

    def test_stato_non_affidabile_blocca_la_scrittura(self):
        self.percorso.write_text("{non json", encoding="utf-8")
        with self.assertRaises(ErrorePreferenzeNonAffidabili):
            app_preferences.imposta_preferenza(self.percorso, "browser", "Firefox")
        # Nessuna scrittura e' avvenuta: il file corrotto resta tale.
        self.assertEqual(self.percorso.read_text(encoding="utf-8"), "{non json")

    def test_persistenza_attraverso_una_nuova_lettura_indipendente(self):
        """Simula una nuova inizializzazione: una chiamata indipendente che
        rilegge da zero vede esattamente cio' che una chiamata precedente
        ha scritto, senza alcuno stato condiviso in RAM tra le due."""

        app_preferences.imposta_preferenza(self.percorso, "media_player", "VLC")

        # "Nuova inizializzazione": nessun oggetto Python riusato, solo
        # il percorso su disco.
        preferenze_rilette = app_preferences.carica_preferenze(self.percorso)
        self.assertEqual(preferenze_rilette["preferences"]["media_player"], "VLC")


if __name__ == "__main__":
    unittest.main()
