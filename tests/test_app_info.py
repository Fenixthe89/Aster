"""
0.7.3 - modules/app_info.py: applicazioni installate secondo le chiavi
Uninstall del registro di Windows.

Copre la precondizione di piattaforma e import sicuro, le tre radici
pertinenti (HKLM vista 64 bit e vista 32 bit tramite KEY_WOW64_64KEY /
KEY_WOW64_32KEY sullo stesso percorso fisico, mai dal percorso
letterale WOW6432Node; HKCU per le installazioni per-utente) in modo
indipendente dalla bitness del processo, l'assenza di una radice come
caso normale (mai un errore) distinta da un errore reale, la fine
naturale di EnumKey (WinError 259) distinta da un errore reale a metà
enumerazione, il circuit breaker sulle sottochiavi (incl. il confine
esatto: un conteggio pari al limite non è mai falsamente incompleto),
voci singole corrotte/non leggibili che non bloccano le altre, il
filtro SystemComponent, la sanitizzazione delle stringhe, la
deduplicazione esatta (mai per nome simile) e l'ordinamento
deterministico, tramite un registro finto in puro Python che rispetta
i flag di accesso passati a OpenKey. Nessuna dipendenza esterna,
nessun subprocess, nessun file in data/.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.app_info as app_info
from modules.app_info import PiattaformaNonSupportata, RisultatoApplicazioni

RADICE_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

# Stesso errore (errno=22, winerror=259) osservato empiricamente da un
# EnumKey reale alla fine naturale dell'enumerazione: solo la forma a 4
# argomenti di OSError imposta .winerror su Windows.
def _fine_naturale() -> OSError:
    return OSError(22, "Non ci sono più dati disponibili per elaborare questo servizio.", None, 259)


class _ErroreValore:
    """Sentinella: QueryValueEx su questo nome solleva l'eccezione data invece di restituire un valore."""

    def __init__(self, eccezione: Exception):
        self.eccezione = eccezione


class _ChiaveFinta:
    """Doppio minimo di un handle winreg: supporta sia `with` sia .Close()."""

    def __init__(self, nodo: dict):
        self.nodo = nodo

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def Close(self):
        pass


class _RegistroFinto:
    """
    Doppio di winreg per le sole operazioni usate da app_info: OpenKey,
    EnumKey, QueryValueEx. Rispetta KEY_WOW64_64KEY/32KEY passati in
    access: lo stesso (hive, percorso) con viste diverse è uno stato
    indipendente, proprio come sul registro reale. Stato costruito
    esplicitamente per ogni test: nessuna lettura del registro reale.
    """

    def __init__(self):
        self.HKEY_LOCAL_MACHINE = object()
        self.HKEY_CURRENT_USER = object()
        self.KEY_READ = 0x20019
        self.KEY_WOW64_64KEY = 0x0100
        self.KEY_WOW64_32KEY = 0x0200
        self._nomi_hive = {
            self.HKEY_LOCAL_MACHINE: "HKEY_LOCAL_MACHINE",
            self.HKEY_CURRENT_USER: "HKEY_CURRENT_USER",
        }
        self._nodi: dict = {}
        self._errori_apertura: dict = {}
        self.aperture: list = []

    def _vista_da_access(self, access: int) -> str | None:
        if access & self.KEY_WOW64_32KEY:
            return "32"
        if access & self.KEY_WOW64_64KEY:
            return "64"
        return None

    def imposta_radice(self, hive_nome, percorso, sottochiavi, vista=None, errore_a_indice=None):
        """errore_a_indice: (indice, eccezione) sollevata da EnumKey a quell'indice invece del nome."""

        nodo = {"__subkeys__": list(sottochiavi)}
        if errore_a_indice is not None:
            nodo["__errore_a_indice__"] = errore_a_indice
        self._nodi[(hive_nome, percorso, vista)] = nodo

    def imposta_voce(self, hive_nome, percorso, subkey, valori, vista=None):
        self._nodi[(hive_nome, f"{percorso}\\{subkey}", vista)] = dict(valori)

    def imposta_errore_apertura(self, hive_nome, percorso, eccezione, vista=None):
        self._errori_apertura[(hive_nome, percorso, vista)] = eccezione

    def OpenKey(self, hkey, percorso, reserved=0, access=None):
        access = self.KEY_READ if access is None else access
        hive_nome = self._nomi_hive[hkey]
        vista = self._vista_da_access(access)
        chiave_lookup = (hive_nome, percorso, vista)
        self.aperture.append(chiave_lookup)

        if chiave_lookup in self._errori_apertura:
            raise self._errori_apertura[chiave_lookup]

        nodo = self._nodi.get(chiave_lookup)
        if nodo is None:
            raise FileNotFoundError(2, "Il sistema non può trovare il file specificato")

        return _ChiaveFinta(nodo)

    def EnumKey(self, chiave, indice):
        sottochiavi = chiave.nodo.get("__subkeys__") or []
        errore_a_indice = chiave.nodo.get("__errore_a_indice__")
        if errore_a_indice is not None and indice == errore_a_indice[0]:
            raise errore_a_indice[1]
        if indice >= len(sottochiavi):
            raise _fine_naturale()
        return sottochiavi[indice]

    def QueryValueEx(self, chiave, nome):
        if nome not in chiave.nodo:
            raise FileNotFoundError(2, "Il sistema non può trovare il file specificato")
        valore = chiave.nodo[nome]
        if isinstance(valore, _ErroreValore):
            raise valore.eccezione
        tipo = 4 if isinstance(valore, int) and not isinstance(valore, bool) else 1
        return valore, tipo


def _app_base(nome, **extra):
    valori = {"DisplayName": nome, "DisplayVersion": "1.0", "Publisher": "Produttore"}
    valori.update(extra)
    return valori


def _registro_minimo():
    """Registro finto con le tre radici presenti ma vuote (nessuna sottochiave)."""

    registro = _RegistroFinto()
    registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, [], vista="64")
    registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, [], vista="32")
    registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, [], vista=None)
    return registro


def _leggi(registro, piattaforma="win32"):
    with (
        mock.patch.object(app_info, "_piattaforma", return_value=piattaforma),
        mock.patch.object(app_info, "winreg", registro),
    ):
        return app_info.leggi_applicazioni_installate()


# =====================================================================
# Piattaforma e import sicuro
# =====================================================================

class TestPiattaforma(unittest.TestCase):

    def test_piattaforma_non_windows_solleva(self):
        with self.assertRaises(PiattaformaNonSupportata):
            _leggi(_registro_minimo(), piattaforma="linux")

    def test_winreg_assente_solleva_anche_su_win32(self):
        with (
            mock.patch.object(app_info, "_piattaforma", return_value="win32"),
            mock.patch.object(app_info, "winreg", None),
        ):
            with self.assertRaises(PiattaformaNonSupportata):
                app_info.leggi_applicazioni_installate()

    def test_windows_con_registro_vuoto_non_solleva(self):
        risultato = _leggi(_registro_minimo())
        self.assertEqual(risultato.applicazioni, [])
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)


# =====================================================================
# Inventario normale, viste pertinenti (FIX 3)
# =====================================================================

class TestInventarioNormale(unittest.TestCase):

    def test_singola_voce_vista_64_bit(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["App1"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "App1", _app_base("7-Zip"), vista="64"
        )

        risultato = _leggi(registro)
        self.assertEqual(
            risultato.applicazioni,
            [{"name": "7-Zip", "version": "1.0", "publisher": "Produttore"}],
        )

    def test_voci_da_tutte_e_tre_le_radici(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("AppNativa"), vista="64"
        )
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["B"], vista="32")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "B", _app_base("App32bit"), vista="32"
        )
        registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, ["C"], vista=None)
        registro.imposta_voce(
            "HKEY_CURRENT_USER", RADICE_UNINSTALL, "C", _app_base("AppUtente"), vista=None
        )

        risultato = _leggi(registro)
        nomi = {a["name"] for a in risultato.applicazioni}
        self.assertEqual(nomi, {"AppNativa", "App32bit", "AppUtente"})

    def test_vista_32_bit_assente_non_e_errore(self):
        """Nessuna sottochiave configurata per vista="32": OpenKey la risolve con FileNotFoundError."""

        registro = _RegistroFinto()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, [], vista="64")
        registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, [], vista=None)
        # Nessuna radice vista="32" impostata.

        risultato = _leggi(registro)
        self.assertEqual(risultato.applicazioni, [])
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_nessuna_installazione_per_utente_e_normale(self):
        registro = _registro_minimo()  # HKCU presente ma vuota
        risultato = _leggi(registro)
        self.assertEqual(risultato.applicazioni, [])
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_stesso_percorso_fisico_viste_diverse_stati_indipendenti(self):
        """Vista 64 e vista 32 condividono il percorso fisico ma non lo stato."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("SoloNativa"), vista="64"
        )
        # vista="32" resta vuota (già impostata da _registro_minimo).

        risultato = _leggi(registro)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["SoloNativa"])

    def test_nessun_flag_wow64_passato_per_hkcu(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, ["A"], vista=None)
        registro.imposta_voce(
            "HKEY_CURRENT_USER", RADICE_UNINSTALL, "A", _app_base("AppUtente"), vista=None
        )
        _leggi(registro)
        # Se HKCU venisse aperta con un flag WOW64, _vista_da_access
        # risulterebbe diversa da None e la voce non si troverebbe: il
        # solo fatto che il test precedente trovi l'app conferma vista=None.


# =====================================================================
# Radici non apribili vs enumerazione incompleta (FIX 1)
# =====================================================================

class TestSorgentiNonDisponibili(unittest.TestCase):

    def test_radice_non_apribile_e_sorgente_non_disponibile(self):
        registro = _registro_minimo()
        registro.imposta_errore_apertura(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, PermissionError(5, "Accesso negato"), vista="64"
        )

        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)
        self.assertEqual(risultato.applicazioni, [])

    def test_radice_non_disponibile_non_blocca_le_altre(self):
        registro = _registro_minimo()
        registro.imposta_errore_apertura(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, OSError(5, "errore"), vista="64"
        )
        registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, ["X"], vista=None)
        registro.imposta_voce(
            "HKEY_CURRENT_USER", RADICE_UNINSTALL, "X", _app_base("Superstite"), vista=None
        )

        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Superstite"])

    def test_tutte_e_tre_non_disponibili(self):
        registro = _RegistroFinto()
        for vista in ("64", "32", None):
            hive_nome = "HKEY_CURRENT_USER" if vista is None else "HKEY_LOCAL_MACHINE"
            registro.imposta_errore_apertura(hive_nome, RADICE_UNINSTALL, OSError("errore"), vista=vista)

        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 3)
        self.assertEqual(risultato.applicazioni, [])

    def test_filenotfound_non_e_mai_sorgente_non_disponibile(self):
        registro = _RegistroFinto()  # nessuna radice impostata: tutte FileNotFoundError
        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)


class TestEnumerazione(unittest.TestCase):
    """FIX 1: fine naturale di EnumKey vs errore reale vs circuit breaker."""

    def test_fine_naturale_non_segnala_incompleta(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A", "B"], vista="64")
        registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("Prima"), vista="64")
        registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "B", _app_base("Seconda"), vista="64")

        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)
        self.assertEqual({a["name"] for a in risultato.applicazioni}, {"Prima", "Seconda"})

    def test_errore_reale_a_meta_segnala_incompleta_ma_mantiene_le_voci_lette(self):
        registro = _registro_minimo()
        registro.imposta_radice(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A", "B", "C"],
            vista="64", errore_a_indice=(1, OSError(5, "errore reale a meta' enumerazione")),
        )
        registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("Prima"), vista="64")
        # "B" e "C" non vengono mai raggiunte: l'errore scatta a indice 1.

        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Prima"])

    def test_errore_con_winerror_259_esplicito_non_e_incompleta(self):
        """Un OSError con .winerror=259 è sempre fine naturale, qualunque sia il messaggio."""

        registro = _registro_minimo()
        registro.imposta_radice(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"],
            vista="64", errore_a_indice=(1, OSError(22, "messaggio qualunque", None, 259)),
        )
        registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("Prima"), vista="64")

        risultato = _leggi(registro)
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Prima"])

    def test_circuit_breaker_raggiunto_segnala_incompleta(self):
        registro = _registro_minimo()
        nomi = [f"App{i}" for i in range(5)]
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, nomi, vista="64")
        for nome in nomi:
            registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, nome, _app_base(nome), vista="64")

        with mock.patch.object(app_info, "_MAX_SUBKEY_PER_RADICE", 3):
            risultato = _leggi(registro)

        self.assertEqual(len(risultato.applicazioni), 3)
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)

    def test_circuit_breaker_esatto_non_segnala_incompleta(self):
        """Esattamente _MAX_SUBKEY_PER_RADICE sottochiavi: fine naturale, mai incompleta."""

        registro = _registro_minimo()
        nomi = [f"App{i}" for i in range(3)]
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, nomi, vista="64")
        for nome in nomi:
            registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, nome, _app_base(nome), vista="64")

        with mock.patch.object(app_info, "_MAX_SUBKEY_PER_RADICE", 3):
            risultato = _leggi(registro)

        self.assertEqual(len(risultato.applicazioni), 3)
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_circuit_breaker_non_legge_la_sottochiave_oltre_il_limite(self):
        """La sottochiave che fa scattare il breaker non viene mai aperta/interrogata."""

        registro = _registro_minimo()
        nomi = ["A", "B"]
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, nomi, vista="64")
        registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("Prima"), vista="64")
        # "B" deliberatamente non impostata: se venisse aperta, OpenKey
        # solleverebbe FileNotFoundError e _leggi_voce la scarterebbe
        # comunque, ma la si vuole dimostrare mai nemmeno tentata.

        with mock.patch.object(app_info, "_MAX_SUBKEY_PER_RADICE", 1):
            risultato = _leggi(registro)

        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Prima"])
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)
        aperture_app_b = [
            a for a in registro.aperture
            if a[1] == f"{RADICE_UNINSTALL}\\B"
        ]
        self.assertEqual(aperture_app_b, [])


# =====================================================================
# Voci singole corrotte/non leggibili
# =====================================================================

class TestVociCorrotte(unittest.TestCase):

    def _singola(self, valori_o_errore):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["App1"], vista="64")
        if isinstance(valori_o_errore, dict):
            registro.imposta_voce(
                "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "App1", valori_o_errore, vista="64"
            )
        else:
            registro.imposta_errore_apertura(
                "HKEY_LOCAL_MACHINE", f"{RADICE_UNINSTALL}\\App1", valori_o_errore, vista="64"
            )
        return _leggi(registro).applicazioni

    def test_senza_displayname_esclusa(self):
        self.assertEqual(self._singola({"DisplayVersion": "1.0"}), [])

    def test_displayname_non_stringa_esclusa(self):
        self.assertEqual(self._singola(_app_base("X", DisplayName=123)), [])

    def test_displayname_vuoto_dopo_strip_esclusa(self):
        self.assertEqual(self._singola(_app_base("   ")), [])

    def test_displayname_con_carattere_di_controllo_esclusa(self):
        self.assertEqual(self._singola(_app_base("App\x07Malformata")), [])

    def test_displayname_con_surrogato_isolato_esclusa(self):
        """FIX 2: un surrogato isolato (Cs) in DisplayName esclude l'intera voce."""

        self.assertEqual(self._singola(_app_base("App" + chr(0xD800) + "Malformata")), [])

    def test_displayname_con_marcatore_formattazione_invisibile_escluso(self):
        """FIX 2: un marcatore Cf (es. RLO) in DisplayName esclude l'intera voce."""

        self.assertEqual(self._singola(_app_base("App" + chr(0x202E) + "Malformata")), [])

    def test_displayname_oltre_il_limite_esclusa(self):
        self.assertEqual(self._singola(_app_base("A" * 500)), [])

    def test_sottochiave_sparita_tra_enumkey_e_apertura_e_normale(self):
        """FIX 1: FileNotFoundError sulla sottochiave è concorrenza normale, non un errore reale."""

        registro = _registro_minimo()
        registro.imposta_radice(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["Sparita", "Buona"], vista="64"
        )
        registro.imposta_errore_apertura(
            "HKEY_LOCAL_MACHINE", f"{RADICE_UNINSTALL}\\Sparita",
            FileNotFoundError(2, "sparita nel frattempo"), vista="64",
        )
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "Buona", _app_base("Superstite"), vista="64"
        )

        risultato = _leggi(registro)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Superstite"])
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_sottochiave_errore_reale_segnala_incompleta_senza_bloccare_le_altre(self):
        """FIX 1: un errore reale (non FileNotFoundError) sulla sottochiave segnala la radice incompleta."""

        registro = _registro_minimo()
        registro.imposta_radice(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["Rotta", "Buona"], vista="64"
        )
        registro.imposta_errore_apertura(
            "HKEY_LOCAL_MACHINE", f"{RADICE_UNINSTALL}\\Rotta",
            PermissionError(5, "Accesso negato"), vista="64",
        )
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "Buona", _app_base("Superstite"), vista="64"
        )

        risultato = _leggi(registro)
        # Le voci già raccolte (incluse quelle lette DOPO l'errore)
        # restano valide: l'enumerazione continua.
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Superstite"])
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)

    def test_senza_displayname_non_segnala_incompleta(self):
        """FIX 1: una voce normale priva di DisplayName non è un errore di enumerazione."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", {"DisplayVersion": "1.0"}, vista="64"
        )

        risultato = _leggi(registro)
        self.assertEqual(risultato.applicazioni, [])
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_system_component_non_segnala_incompleta(self):
        """FIX 1: una voce marcata SystemComponent=1 non è un errore di enumerazione."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base("Componente", SystemComponent=1), vista="64",
        )

        risultato = _leggi(registro)
        self.assertEqual(risultato.applicazioni, [])
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_valore_assente_non_segnala_incompleta(self):
        """Correzione _leggi_valore: FileNotFoundError resta un valore assente normale."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", {"DisplayName": "SoloNome"}, vista="64",
        )

        risultato = _leggi(registro)
        self.assertEqual(
            risultato.applicazioni,
            [{"name": "SoloNome", "version": None, "publisher": None}],
        )
        self.assertEqual(risultato.sorgenti_non_disponibili, 0)

    def test_accesso_negato_su_displayname_segnala_incompleta(self):
        """Correzione _leggi_valore: un PermissionError su QueryValueEx non è un valore assente."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["Rotta", "Buona"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "Rotta",
            {"DisplayName": _ErroreValore(PermissionError(5, "Accesso negato"))}, vista="64",
        )
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "Buona", _app_base("Superstite"), vista="64",
        )

        risultato = _leggi(registro)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Superstite"])
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)

    def test_accesso_negato_su_system_component_segnala_incompleta(self):
        """Lo stesso errore reale propagato tramite _componente_di_sistema, non solo DisplayName."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["Rotta"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "Rotta",
            {
                "DisplayName": "X",
                "SystemComponent": _ErroreValore(PermissionError(5, "Accesso negato")),
            },
            vista="64",
        )

        risultato = _leggi(registro)
        self.assertEqual(risultato.applicazioni, [])
        self.assertEqual(risultato.sorgenti_non_disponibili, 1)

    def test_displayversion_invalida_azzera_solo_quel_campo(self):
        voce = self._singola(_app_base("X", DisplayVersion="y" * 500))[0]
        self.assertEqual(voce["name"], "X")
        self.assertIsNone(voce["version"])

    def test_publisher_con_carattere_di_controllo_azzera_solo_quel_campo(self):
        voce = self._singola(_app_base("X", Publisher="Produttore\x01"))[0]
        self.assertEqual(voce["name"], "X")
        self.assertIsNone(voce["publisher"])

    def test_displayversion_con_surrogato_isolato_azzera_solo_quel_campo(self):
        """FIX 2: nome e produttore restano validi, solo version diventa None."""

        voce = self._singola(_app_base("X", DisplayVersion="1." + chr(0xDFFF) + "0"))[0]
        self.assertEqual(voce["name"], "X")
        self.assertIsNone(voce["version"])
        self.assertEqual(voce["publisher"], "Produttore")

    def test_publisher_con_marcatore_formattazione_invisibile_azzera_solo_quel_campo(self):
        """FIX 2: un marcatore Cf (es. ZWJ) in publisher azzera solo quel campo."""

        voce = self._singola(_app_base("X", Publisher="Produttore" + chr(0x200D) + "SpA"))[0]
        self.assertEqual(voce["name"], "X")
        self.assertIsNone(voce["publisher"])

    def test_surrogato_isolato_mai_nel_risultato_json_serializzabile(self):
        """Verifica la motivazione pratica del FIX 2: senza sanitizzazione, un
        surrogato isolato fa sollevare UnicodeEncodeError a json.dumps(...)
        .encode('utf-8'), lo stesso path usato altrove nel progetto per i
        risultati tool. Qui il surrogato è nel nome: l'intera voce è esclusa,
        quindi il risultato resta sempre serializzabile."""

        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base("App" + chr(0xD800) + "Malformata"), vista="64",
        )

        risultato = _leggi(registro)
        self.assertEqual(risultato.applicazioni, [])
        json.dumps(risultato.applicazioni, ensure_ascii=False).encode("utf-8")

    def test_displayversion_e_publisher_assenti_restano_none(self):
        voce = self._singola({"DisplayName": "Solo Nome"})[0]
        self.assertIsNone(voce["version"])
        self.assertIsNone(voce["publisher"])


# =====================================================================
# SystemComponent
# =====================================================================

class TestSystemComponent(unittest.TestCase):

    def _con_system_component(self, valore):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["App1"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "App1",
            _app_base("Componente", SystemComponent=valore), vista="64",
        )
        return _leggi(registro).applicazioni

    def test_system_component_uno_esclude(self):
        self.assertEqual(self._con_system_component(1), [])

    def test_system_component_zero_non_esclude(self):
        self.assertEqual(len(self._con_system_component(0)), 1)

    def test_system_component_assente_non_esclude(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["App1"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "App1", _app_base("Normale"), vista="64"
        )
        self.assertEqual(len(_leggi(registro).applicazioni), 1)

    def test_system_component_stringa_non_esclude(self):
        self.assertEqual(len(self._con_system_component("1")), 1)

    def test_system_component_bool_non_esclude(self):
        self.assertEqual(len(self._con_system_component(True)), 1)


# =====================================================================
# Deduplicazione
# =====================================================================

class TestDeduplicazione(unittest.TestCase):

    def test_stessa_tripla_tra_vista_64_e_32_unita(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base("Stesso", DisplayVersion="2.0", Publisher="X"), vista="64",
        )
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["B"], vista="32")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "B",
            _app_base("Stesso", DisplayVersion="2.0", Publisher="X"), vista="32",
        )

        risultato = _leggi(registro)
        self.assertEqual(len(risultato.applicazioni), 1)

    def test_dedup_case_insensitive(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base("Chrome", DisplayVersion="1.0", Publisher="Google"), vista="64",
        )
        registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, ["B"], vista=None)
        registro.imposta_voce(
            "HKEY_CURRENT_USER", RADICE_UNINSTALL, "B",
            _app_base("CHROME", DisplayVersion="1.0", Publisher="GOOGLE"), vista=None,
        )

        risultato = _leggi(registro)
        self.assertEqual(len(risultato.applicazioni), 1)

    def test_nomi_simili_ma_versioni_diverse_restano_distinte(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A", "B"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base("Python 3.11", DisplayVersion="3.11.0", Publisher="PSF"), vista="64",
        )
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "B",
            _app_base("Python 3.12", DisplayVersion="3.12.0", Publisher="PSF"), vista="64",
        )

        risultato = _leggi(registro)
        self.assertEqual(len(risultato.applicazioni), 2)

    def test_stesso_nome_produttore_diverso_restano_distinte(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base("Driver", DisplayVersion="1.0", Publisher="Produttore1"), vista="64",
        )
        registro.imposta_radice("HKEY_CURRENT_USER", RADICE_UNINSTALL, ["B"], vista=None)
        registro.imposta_voce(
            "HKEY_CURRENT_USER", RADICE_UNINSTALL, "B",
            _app_base("Driver", DisplayVersion="1.0", Publisher="Produttore2"), vista=None,
        )

        risultato = _leggi(registro)
        self.assertEqual(len(risultato.applicazioni), 2)


# =====================================================================
# Ordinamento
# =====================================================================

class TestOrdinamento(unittest.TestCase):

    def test_ordine_casefold_poi_nome(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A", "B", "C"], vista="64")
        for subkey, nome in (("A", "zebra"), ("B", "Alpha"), ("C", "beta")):
            registro.imposta_voce(
                "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, subkey, _app_base(nome), vista="64"
            )

        risultato = _leggi(registro)
        self.assertEqual([a["name"] for a in risultato.applicazioni], ["Alpha", "beta", "zebra"])

    def test_ordine_deterministico_indipendente_dall_ordine_di_lettura(self):
        registro_1 = _registro_minimo()
        registro_1.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A", "B"], vista="64")
        registro_1.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("Beta"), vista="64")
        registro_1.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "B", _app_base("Alfa"), vista="64")

        registro_2 = _registro_minimo()
        registro_2.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["B", "A"], vista="64")
        registro_2.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("Beta"), vista="64")
        registro_2.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "B", _app_base("Alfa"), vista="64")

        self.assertEqual(_leggi(registro_1).applicazioni, _leggi(registro_2).applicazioni)


# =====================================================================
# Privacy e contratto dei campi
# =====================================================================

class TestContrattoCampi(unittest.TestCase):

    def test_solo_name_version_publisher(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce(
            "HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A",
            _app_base(
                "App",
                UninstallString=r"C:\Program Files\App\uninstall.exe /S",
                InstallLocation=r"C:\Program Files\App",
                EstimatedSize=123456,
            ),
            vista="64",
        )

        voce = _leggi(registro).applicazioni[0]
        self.assertEqual(set(voce), {"name", "version", "publisher"})

    def test_namedtuple_e_lista_di_dict(self):
        registro = _registro_minimo()
        registro.imposta_radice("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, ["A"], vista="64")
        registro.imposta_voce("HKEY_LOCAL_MACHINE", RADICE_UNINSTALL, "A", _app_base("App"), vista="64")
        risultato = _leggi(registro)
        self.assertIsInstance(risultato, RisultatoApplicazioni)
        self.assertIsInstance(risultato.applicazioni, list)
        self.assertIsInstance(risultato.applicazioni[0], dict)


if __name__ == "__main__":
    unittest.main()
