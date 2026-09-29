"""
0.7.2c - modules/volume_info.py: unità locali fisse e loro spazio.

Copre precondizioni di piattaforma e import sicuro, conversione della
bitmask di GetLogicalDrives, classificazione GetDriveTypeW (solo
DRIVE_FIXED arriva alla lettura dello spazio), error mode del thread
ripristinato sempre, normalizzazione dei valori, percentuale troncata,
limite di 8 volumi con totale e troncamento, tramite una API finta in
puro Python. Su Windows: caricamento di kernel32 da System32 e un unico
smoke reale senza lettere o capacità hardcodate.

Nessuna dipendenza esterna, nessun subprocess, nessun file in data/.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import collections
import ctypes
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.volume_info as volume_info
from modules.volume_info import (
    DRIVE_CDROM,
    DRIVE_FIXED,
    DRIVE_NO_ROOT_DIR,
    DRIVE_RAMDISK,
    DRIVE_REMOTE,
    DRIVE_REMOVABLE,
    DRIVE_UNKNOWN,
    EnumerazioneNonRiuscita,
    PiattaformaNonSupportata,
    RisultatoVolumi,
)

WINDOWS = sys.platform == "win32"
SEM_FAILCRITICALERRORS = 0x0001
MODALITA_INIZIALE = 0x8000

Uso = collections.namedtuple("Uso", "total used free")
USO_BASE = Uso(1000, 250, 750)

CAMPI_VOLUME = {
    "drive",
    "info_available",
    "total_bytes",
    "used_bytes",
    "free_bytes",
    "used_percent",
}


def _maschera(*lettere):
    maschera = 0
    for lettera in lettere:
        maschera |= 1 << (ord(lettera) - ord("A"))
    return maschera


class _ApiFinta:
    """
    Doppio di _ApiKernel32. tipi: lettera -> tipo (default DRIVE_FIXED).
    spazi: lettera -> Uso o eccezione da sollevare (default USO_BASE).
    modalita_fallisce: ogni SetThreadErrorMode fallisce; chiamate_modalita_fallite:
    indici (0-based) delle sole chiamate SetThreadErrorMode che falliscono.
    Registra ogni chiamata e l'error mode attivo durante la lettura dello spazio.
    """

    def __init__(self, lettere=(), errore=0, maschera=None, tipi=None, spazi=None,
                 modalita_fallisce=False, chiamate_modalita_fallite=()):
        self._maschera = _maschera(*lettere) if maschera is None else maschera
        self._errore = errore
        self._tipi = tipi or {}
        self._spazi = spazi or {}
        self._modalita_fallisce = modalita_fallisce
        self._chiamate_modalita_fallite = set(chiamate_modalita_fallite)
        self.modalita = MODALITA_INIZIALE
        self.chiamate_tipo = []
        self.chiamate_spazio = []
        self.modalita_durante_spazio = []
        self.chiamate_modalita = []

    def lettere_logiche(self):
        return self._maschera, self._errore

    def tipo_unita(self, radice):
        self.chiamate_tipo.append(radice)
        return self._tipi.get(radice[0], DRIVE_FIXED)

    def imposta_modalita_errori(self, modalita):
        indice = len(self.chiamate_modalita)
        self.chiamate_modalita.append(modalita)
        if self._modalita_fallisce or indice in self._chiamate_modalita_fallite:
            return None
        precedente = self.modalita
        self.modalita = modalita
        return precedente

    def spazio(self, radice):
        self.chiamate_spazio.append(radice)
        self.modalita_durante_spazio.append(self.modalita)
        valore = self._spazi.get(radice[0], USO_BASE)
        if isinstance(valore, BaseException):
            raise valore
        return valore


def _leggi(api):
    return volume_info._leggi(api)


def _lettere_interrogate(api):
    return [radice[0] for radice in api.chiamate_spazio]


# =====================================================================
# Import e piattaforma
# =====================================================================

class TestImportEPiattaforma(unittest.TestCase):

    def test_import_non_carica_kernel32(self):
        def vietato(*args, **kwargs):
            raise AssertionError("nessun accesso a kernel32 all'import")

        spec = importlib.util.spec_from_file_location(
            "volume_info_import_isolato",
            BASE_DIR / "modules" / "volume_info.py",
        )
        modulo = importlib.util.module_from_spec(spec)
        with mock.patch.object(ctypes, "WinDLL", vietato, create=True):
            spec.loader.exec_module(modulo)

        self.assertTrue(callable(modulo.leggi_volumi_locali))

    def test_piattaforme_non_windows_non_supportate(self):
        for piattaforma in ("linux", "darwin", "freebsd14"):
            with self.subTest(piattaforma=piattaforma):
                with (
                    mock.patch.object(volume_info, "_piattaforma", return_value=piattaforma),
                    mock.patch.object(volume_info, "_ApiKernel32",
                                      side_effect=AssertionError("kernel32 vietata")),
                ):
                    with self.assertRaises(PiattaformaNonSupportata):
                        volume_info.leggi_volumi_locali()

    def test_windows_usa_il_backend(self):
        api = _ApiFinta(("C",))
        with (
            mock.patch.object(volume_info, "_piattaforma", return_value="win32"),
            mock.patch.object(volume_info, "_ApiKernel32", return_value=api),
        ):
            risultato = volume_info.leggi_volumi_locali()

        self.assertIsInstance(risultato, RisultatoVolumi)
        self.assertEqual(risultato.totale, 1)

    def test_sorgente_senza_primitive_vietate(self):
        sorgente = (BASE_DIR / "modules" / "volume_info.py").read_text(encoding="utf-8")
        for vietato in (
            "import subprocess",
            "subprocess.",
            "os.system",
            "os.popen",
            "import wmi",
            "win32com",
            "GetVolumeInformation",
            "GetVolumeNameForVolumeMountPoint",
            "QueryDosDevice",
            "WNetGetConnection",
            "listdir",
            "scandir",
            "open(",
            "eval(",
            "exec(",
        ):
            self.assertNotIn(vietato, sorgente, msg=vietato)


# =====================================================================
# GetLogicalDrives
# =====================================================================

class TestBitmask(unittest.TestCase):

    def test_bitmask_in_lettere(self):
        risultato = _leggi(_ApiFinta(("C", "D", "Z")))
        self.assertEqual([v["drive"] for v in risultato.volumi], ["C:", "D:", "Z:"])

    def test_ordine_a_z(self):
        api = _ApiFinta(("Z", "A", "M"))
        _leggi(api)
        self.assertEqual(api.chiamate_tipo, ["A:\\", "M:\\", "Z:\\"])

    def test_bit_oltre_z_ignorati(self):
        api = _ApiFinta(maschera=_maschera("C") | (1 << 26) | (1 << 31))
        risultato = _leggi(api)
        self.assertEqual(api.chiamate_tipo, ["C:\\"])
        self.assertEqual(risultato.totale, 1)

    def test_maschera_zero_senza_errore_enumerazione_vuota(self):
        risultato = _leggi(_ApiFinta(maschera=0, errore=0))
        self.assertEqual(risultato, RisultatoVolumi([], 0, False))

    def test_maschera_zero_con_errore_enumerazione_fallita(self):
        api = _ApiFinta(maschera=0, errore=5)
        with self.assertRaises(EnumerazioneNonRiuscita):
            _leggi(api)
        self.assertEqual(api.chiamate_tipo, [])

    def test_errore_residuo_ignorato_con_maschera_valida(self):
        risultato = _leggi(_ApiFinta(("C",), errore=87))
        self.assertEqual(risultato.totale, 1)

    def test_formato_drive_senza_backslash(self):
        for volume in _leggi(_ApiFinta(("C", "D"))).volumi:
            self.assertRegex(volume["drive"], r"^[A-Z]:$")


# =====================================================================
# GetDriveTypeW: solo DRIVE_FIXED arriva a disk_usage
# =====================================================================

class TestClassificazione(unittest.TestCase):

    def _solo(self, tipo):
        api = _ApiFinta(("C", "X"), tipi={"X": tipo})
        risultato = _leggi(api)
        return api, risultato

    def test_solo_fixed_arriva_alla_lettura_dello_spazio(self):
        tipi = {
            "A": DRIVE_UNKNOWN, "B": DRIVE_NO_ROOT_DIR, "C": DRIVE_FIXED,
            "D": DRIVE_REMOVABLE, "E": DRIVE_REMOTE, "F": DRIVE_CDROM,
            "G": DRIVE_RAMDISK, "H": DRIVE_FIXED, "I": 99,
        }
        api = _ApiFinta(tuple(tipi), tipi=tipi)
        risultato = _leggi(api)

        self.assertEqual(_lettere_interrogate(api), ["C", "H"])
        self.assertEqual([v["drive"] for v in risultato.volumi], ["C:", "H:"])
        self.assertEqual(risultato.totale, 2)
        # Ogni lettera è classificata, anche quelle poi escluse.
        self.assertEqual(len(api.chiamate_tipo), len(tipi))

    def test_rete_non_interrogata(self):
        api, risultato = self._solo(DRIVE_REMOTE)
        self.assertEqual(_lettere_interrogate(api), ["C"])
        self.assertEqual(risultato.totale, 1)

    def test_rimovibile_non_interrogata(self):
        api, risultato = self._solo(DRIVE_REMOVABLE)
        self.assertEqual(_lettere_interrogate(api), ["C"])
        self.assertEqual(risultato.totale, 1)

    def test_cdrom_non_interrogata(self):
        api, risultato = self._solo(DRIVE_CDROM)
        self.assertEqual(_lettere_interrogate(api), ["C"])
        self.assertEqual(risultato.totale, 1)

    def test_ramdisk_non_interrogata(self):
        api, risultato = self._solo(DRIVE_RAMDISK)
        self.assertEqual(_lettere_interrogate(api), ["C"])
        self.assertEqual(risultato.totale, 1)

    def test_sconosciuta_e_root_non_valida_non_interrogate(self):
        for tipo in (DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, 7, 255):
            with self.subTest(tipo=tipo):
                api, risultato = self._solo(tipo)
                self.assertEqual(_lettere_interrogate(api), ["C"])
                self.assertEqual(risultato.totale, 1)

    def test_nessuna_fixed_elenco_vuoto(self):
        api = _ApiFinta(("D", "E"), tipi={"D": DRIVE_REMOVABLE, "E": DRIVE_REMOTE})
        self.assertEqual(_leggi(api), RisultatoVolumi([], 0, False))
        self.assertEqual(api.chiamate_spazio, [])

    def test_valori_costanti_windows(self):
        self.assertEqual(
            (DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE, DRIVE_FIXED,
             DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK),
            (0, 1, 2, 3, 4, 5, 6),
        )


# =====================================================================
# Lettura dello spazio ed error mode
# =====================================================================

class TestLetturaSpazio(unittest.TestCase):

    def test_errore_su_un_volume_non_ferma_gli_altri(self):
        api = _ApiFinta(("C", "D", "E"), spazi={"D": OSError(21, "Il dispositivo non è pronto", "D:\\")})
        risultato = _leggi(api)

        self.assertEqual([v["info_available"] for v in risultato.volumi], [True, False, True])
        self.assertEqual(risultato.totale, 3)
        self.assertEqual(_lettere_interrogate(api), ["C", "D", "E"])

    def test_errore_generico_su_un_volume(self):
        api = _ApiFinta(("C",), spazi={"C": RuntimeError("x")})
        volume = _leggi(api).volumi[0]
        self.assertFalse(volume["info_available"])

    def test_modalita_errori_attiva_durante_la_lettura(self):
        api = _ApiFinta(("C", "D"))
        _leggi(api)
        self.assertEqual(api.modalita_durante_spazio, [SEM_FAILCRITICALERRORS] * 2)

    def test_modalita_ripristinata(self):
        api = _ApiFinta(("C",))
        _leggi(api)
        self.assertEqual(api.modalita, MODALITA_INIZIALE)
        self.assertEqual(api.chiamate_modalita, [SEM_FAILCRITICALERRORS, MODALITA_INIZIALE])

    def test_modalita_ripristinata_anche_su_eccezione(self):
        api = _ApiFinta(("C",), spazi={"C": OSError("x")})
        _leggi(api)
        self.assertEqual(api.modalita, MODALITA_INIZIALE)
        self.assertEqual(api.chiamate_modalita, [SEM_FAILCRITICALERRORS, MODALITA_INIZIALE])

    def test_attivazione_fallita_nessuna_lettura_fail_closed(self):
        api = _ApiFinta(("C", "D"), modalita_fallisce=True)
        risultato = _leggi(api)

        # Senza SEM_FAILCRITICALERRORS lo spazio non viene mai letto.
        self.assertEqual(api.chiamate_spazio, [])
        self.assertEqual(
            risultato.volumi,
            [volume_info._volume_illeggibile("C:"), volume_info._volume_illeggibile("D:")],
        )
        self.assertEqual(risultato.totale, 2)
        # Nessun ripristino tentato: l'error mode non è mai stato cambiato.
        self.assertEqual(api.chiamate_modalita, [SEM_FAILCRITICALERRORS, SEM_FAILCRITICALERRORS])

    def test_attivazione_fallita_nessuna_eccezione_raw(self):
        api = _ApiFinta(("C",), modalita_fallisce=True)
        risultato = _leggi(api)

        self.assertIsInstance(risultato, RisultatoVolumi)
        serializzato = json.dumps(risultato.volumi, allow_nan=False)
        self.assertNotIn("Error mode", serializzato)
        self.assertNotIn("Modalita", serializzato)

    def test_attivazione_fallita_solo_su_un_volume(self):
        # Fallisce solo l'attivazione di D: (terza chiamata: C set, C restore, D set).
        api = _ApiFinta(("C", "D", "E"), chiamate_modalita_fallite={2})
        risultato = _leggi(api)

        self.assertEqual(_lettere_interrogate(api), ["C", "E"])
        self.assertEqual([v["info_available"] for v in risultato.volumi], [True, False, True])

    def test_attivazione_riuscita_lettura_normale(self):
        api = _ApiFinta(("C",))
        risultato = _leggi(api)

        self.assertEqual(api.chiamate_spazio, ["C:\\"])
        self.assertTrue(risultato.volumi[0]["info_available"])
        self.assertEqual(api.chiamate_modalita, [SEM_FAILCRITICALERRORS, MODALITA_INIZIALE])

    def test_ripristino_fallito_volume_non_presentato_come_riuscito(self):
        # C: set ok, restore fallito (indice 1); D: set e restore ok.
        api = _ApiFinta(("C", "D"), chiamate_modalita_fallite={1})
        risultato = _leggi(api)

        self.assertEqual(_lettere_interrogate(api), ["C", "D"])
        self.assertEqual(risultato.volumi[0], volume_info._volume_illeggibile("C:"))
        # Gli altri volumi non cambiano comportamento: D: resta leggibile e protetto.
        self.assertTrue(risultato.volumi[1]["info_available"])
        self.assertEqual(api.modalita_durante_spazio, [SEM_FAILCRITICALERRORS, SEM_FAILCRITICALERRORS])
        json.dumps(risultato.volumi, allow_nan=False)

    def test_ripristino_fallito_dopo_errore_di_lettura(self):
        api = _ApiFinta(("C",), spazi={"C": OSError(21, "non pronto", "C:\\")},
                        chiamate_modalita_fallite={1})
        risultato = _leggi(api)

        self.assertEqual(risultato.volumi, [volume_info._volume_illeggibile("C:")])
        self.assertEqual(api.chiamate_modalita, [SEM_FAILCRITICALERRORS, MODALITA_INIZIALE])

    def test_leggi_volumi_locali_nessuna_eccezione_con_modalita_rotta(self):
        api = _ApiFinta(("C", "D"), modalita_fallisce=True)
        with (
            mock.patch.object(volume_info, "_piattaforma", return_value="win32"),
            mock.patch.object(volume_info, "_ApiKernel32", return_value=api),
        ):
            risultato = volume_info.leggi_volumi_locali()

        self.assertEqual([v["info_available"] for v in risultato.volumi], [False, False])
        self.assertIsNone(volume_info.unita_piu_piena(risultato.volumi, risultato.troncato))

    def test_radice_passata_con_backslash(self):
        api = _ApiFinta(("C",))
        _leggi(api)
        self.assertEqual(api.chiamate_spazio, ["C:\\"])


# =====================================================================
# Normalizzazione
# =====================================================================

class TestNormalizzazione(unittest.TestCase):

    def _volume(self, uso):
        return _leggi(_ApiFinta(("C",), spazi={"C": uso})).volumi[0]

    def _illeggibile(self, volume):
        self.assertEqual(
            volume,
            {"drive": "C:", "info_available": False, "total_bytes": None,
             "used_bytes": None, "free_bytes": None, "used_percent": None},
        )

    def test_valori_normali(self):
        self.assertEqual(
            self._volume(Uso(2_000_000_000_000, 1_385_000_000_000, 615_000_000_000)),
            {"drive": "C:", "info_available": True, "total_bytes": 2_000_000_000_000,
             "used_bytes": 1_385_000_000_000, "free_bytes": 615_000_000_000, "used_percent": 69},
        )

    def test_campi_esatti(self):
        self.assertEqual(set(self._volume(USO_BASE)), CAMPI_VOLUME)

    def test_totale_zero(self):
        self._illeggibile(self._volume(Uso(0, 0, 0)))

    def test_bool_non_valido(self):
        for uso in (Uso(True, 0, 1), Uso(10, True, 9), Uso(10, 9, True), Uso(True, True, False)):
            with self.subTest(uso=uso):
                self._illeggibile(self._volume(uso))

    def test_float_non_valido(self):
        for uso in (Uso(1000.0, 250, 750), Uso(1000, 250.0, 750), Uso(1000, 250, float("nan"))):
            with self.subTest(uso=uso):
                self._illeggibile(self._volume(uso))

    def test_altri_tipi_non_validi(self):
        for uso in (Uso("1000", 250, 750), Uso(None, None, None), Uso(1000, [250], 750)):
            with self.subTest(uso=uso):
                self._illeggibile(self._volume(uso))

    def test_negativo(self):
        for uso in (Uso(-1000, -250, -750), Uso(1000, -250, 1250), Uso(1000, 1250, -250)):
            with self.subTest(uso=uso):
                self._illeggibile(self._volume(uso))

    def test_libero_oltre_il_totale(self):
        self._illeggibile(self._volume(Uso(1000, 0, 1001)))

    def test_usato_oltre_il_totale(self):
        self._illeggibile(self._volume(Uso(1000, 1001, 0)))

    def test_somma_incoerente(self):
        for uso in (Uso(1000, 250, 700), Uso(1000, 300, 750)):
            with self.subTest(uso=uso):
                self._illeggibile(self._volume(uso))

    def test_oggetto_senza_campi(self):
        self._illeggibile(self._volume(SimpleNamespace(total=1000)))

    def test_mai_correzioni_ne_zeri_inventati(self):
        volume = self._volume(Uso(1000, 300, 750))
        self.assertIsNone(volume["free_bytes"])
        self.assertIsNone(volume["used_percent"])

    def test_percentuale_troncata(self):
        casi = {
            Uso(3, 2, 1): 66,
            Uso(1000, 999, 1): 99,
            Uso(1000, 695, 305): 69,
            Uso(10 ** 13, 1, 10 ** 13 - 1): 0,
            Uso(1000, 0, 1000): 0,
        }
        for uso, atteso in casi.items():
            with self.subTest(uso=uso):
                volume = self._volume(uso)
                self.assertEqual(volume["used_percent"], atteso)
                self.assertIs(type(volume["used_percent"]), int)

    def test_cento_solo_se_pieno(self):
        self.assertEqual(self._volume(Uso(1000, 1000, 0))["used_percent"], 100)
        self.assertEqual(self._volume(Uso(10 ** 12, 10 ** 12 - 1, 1))["used_percent"], 99)

    def test_zero_libero_valido(self):
        volume = self._volume(Uso(500, 500, 0))
        self.assertTrue(volume["info_available"])
        self.assertEqual(volume["free_bytes"], 0)


# =====================================================================
# Limite, totale e troncamento
# =====================================================================

class TestLimite(unittest.TestCase):

    def test_valore_del_limite(self):
        self.assertEqual(volume_info.MAX_LOCAL_VOLUMES, 8)

    def test_massimo_otto(self):
        lettere = tuple("CDEFGHIJKL")
        api = _ApiFinta(lettere)
        risultato = _leggi(api)

        self.assertEqual(len(risultato.volumi), 8)
        self.assertEqual(risultato.totale, 10)
        self.assertTrue(risultato.troncato)
        self.assertEqual([v["drive"] for v in risultato.volumi], [f"{l}:" for l in lettere[:8]])
        # Spazio letto solo per i volumi restituiti.
        self.assertEqual(_lettere_interrogate(api), list(lettere[:8]))

    def test_esattamente_otto_non_troncato(self):
        risultato = _leggi(_ApiFinta(tuple("CDEFGHIJ")))
        self.assertEqual((len(risultato.volumi), risultato.totale, risultato.troncato), (8, 8, False))

    def test_totale_conta_solo_le_fixed(self):
        lettere = tuple("CDEFGHIJK")
        risultato = _leggi(_ApiFinta(lettere, tipi={"K": DRIVE_REMOTE}))
        self.assertEqual((len(risultato.volumi), risultato.totale, risultato.troncato), (8, 8, False))

    def test_volume_illeggibile_conta_nel_totale_e_appare(self):
        lettere = tuple("CDEFGHIJKL")
        risultato = _leggi(_ApiFinta(lettere, spazi={"D": OSError("x")}))
        self.assertEqual(risultato.totale, 10)
        self.assertFalse(risultato.volumi[1]["info_available"])
        self.assertEqual(risultato.volumi[1]["drive"], "D:")

    def test_non_troncato_con_pochi_volumi(self):
        risultato = _leggi(_ApiFinta(("C", "D")))
        self.assertFalse(risultato.troncato)

    def test_json_serializzabile(self):
        risultato = _leggi(_ApiFinta(tuple("CDEFGHIJKL"), spazi={"E": OSError("x")}))
        json.dumps(risultato.volumi, allow_nan=False)


# =====================================================================
# Unità più piena (fullest_drive), calcolata da Python
# =====================================================================

def _vp(lettera, percento):
    """Volume normalizzato da 1000 byte con used_percent esatto."""

    return volume_info._normalizza_volume(f"{lettera}:", 1000, percento * 10, 1000 - percento * 10)


class TestUnitaPiuPiena(unittest.TestCase):

    def test_a_percentuale_non_byte_assoluti(self):
        volumi = [
            volume_info._normalizza_volume("C:", 1_998_880_501_760, 1_385_637_511_168, 613_242_990_592),
            volume_info._normalizza_volume("D:", 4_000_768_323_584, 2_518_489_477_120, 1_482_278_846_464),
        ]
        self.assertEqual([v["used_percent"] for v in volumi], [69, 62])
        self.assertEqual(volume_info.unita_piu_piena(volumi, False), "C:")

    def test_b_seconda_unita(self):
        self.assertEqual(volume_info.unita_piu_piena([_vp("C", 40), _vp("D", 85)], False), "D:")

    def test_c_parita_prima_a_z(self):
        self.assertEqual(volume_info.unita_piu_piena([_vp("C", 69), _vp("D", 69)], False), "C:")

    def test_parita_a_z_indipendente_dall_ordine(self):
        self.assertEqual(volume_info.unita_piu_piena([_vp("E", 69), _vp("C", 69)], False), "C:")

    def test_d_illeggibile_ignorata(self):
        volumi = [volume_info._volume_illeggibile("C:"), _vp("D", 62)]
        self.assertEqual(volume_info.unita_piu_piena(volumi, False), "D:")

    def test_e_tutte_illeggibili(self):
        volumi = [volume_info._volume_illeggibile("C:"), volume_info._volume_illeggibile("D:")]
        self.assertIsNone(volume_info.unita_piu_piena(volumi, False))

    def test_f_lista_vuota(self):
        self.assertIsNone(volume_info.unita_piu_piena([], False))

    def test_g_troncato(self):
        volumi = [_vp(lettera, 10 + indice) for indice, lettera in enumerate("CDEFGHIJ")]
        self.assertIsNone(volume_info.unita_piu_piena(volumi, True))

    def test_h_un_solo_volume_valido(self):
        self.assertEqual(volume_info.unita_piu_piena([_vp("E", 5)], False), "E:")

    def test_cento_e_zero_validi(self):
        self.assertEqual(volume_info.unita_piu_piena([_vp("C", 0), _vp("D", 100)], False), "D:")
        self.assertEqual(volume_info.unita_piu_piena([_vp("C", 0)], False), "C:")

    def test_valori_non_validi_ignorati(self):
        for percentuale in (True, 69.0, "69", None, -1, 101):
            with self.subTest(percentuale=percentuale):
                volumi = [{**_vp("C", 50), "used_percent": percentuale}, _vp("D", 20)]
                self.assertEqual(volume_info.unita_piu_piena(volumi, False), "D:")

    def test_info_available_non_true_ignorato(self):
        for valore in (False, "true", 1, None):
            with self.subTest(valore=valore):
                volumi = [{**_vp("C", 90), "info_available": valore}, _vp("D", 20)]
                self.assertEqual(volume_info.unita_piu_piena(volumi, False), "D:")

    def test_elementi_non_validi_ignorati(self):
        volumi = ["non un dict", {**_vp("C", 90), "drive": None}, _vp("D", 20)]
        self.assertEqual(volume_info.unita_piu_piena(volumi, False), "D:")

    def test_lettura_reale_non_modificata(self):
        volumi = [_vp("C", 10), _vp("D", 90)]
        copia = [dict(v) for v in volumi]
        volume_info.unita_piu_piena(volumi, False)
        self.assertEqual(volumi, copia)


# =====================================================================
# Strato ctypes (Windows): kernel32 da System32 e spazio via shutil
# =====================================================================

class TestStratoCtypes(unittest.TestCase):

    def _funzioni_finte(self):
        return SimpleNamespace(
            GetLogicalDrives=mock.Mock(return_value=0),
            GetDriveTypeW=mock.Mock(return_value=DRIVE_FIXED),
            SetThreadErrorMode=mock.Mock(return_value=0),
        )

    def test_kernel32_solo_da_system32(self):
        chiamate = []
        funzioni = self._funzioni_finte()

        def windll_finto(nome, **kwargs):
            chiamate.append((nome, kwargs))
            return funzioni

        with mock.patch.object(ctypes, "WinDLL", windll_finto, create=True):
            volume_info._ApiKernel32()

        self.assertEqual(
            chiamate,
            [("kernel32", {"use_last_error": True, "winmode": 0x00000800})],
        )
        self.assertIs(funzioni.GetLogicalDrives.restype, ctypes.c_uint32)
        self.assertIs(funzioni.GetDriveTypeW.restype, ctypes.c_uint)

    def test_codice_errore_letto_solo_con_maschera_zero(self):
        funzioni = self._funzioni_finte()
        with mock.patch.object(ctypes, "WinDLL", lambda *a, **k: funzioni, create=True):
            api = volume_info._ApiKernel32()
        with (
            mock.patch.object(ctypes, "set_last_error", create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=5, create=True),
        ):
            funzioni.GetLogicalDrives.return_value = 0
            self.assertEqual(api.lettere_logiche(), (0, 5))
            funzioni.GetLogicalDrives.return_value = _maschera("C")
            self.assertEqual(api.lettere_logiche(), (_maschera("C"), 0))

    def test_impostazione_modalita_fallita_restituisce_none(self):
        funzioni = self._funzioni_finte()
        with mock.patch.object(ctypes, "WinDLL", lambda *a, **k: funzioni, create=True):
            api = volume_info._ApiKernel32()
        self.assertIsNone(api.imposta_modalita_errori(SEM_FAILCRITICALERRORS))

    def test_spazio_usa_shutil_disk_usage(self):
        funzioni = self._funzioni_finte()
        with mock.patch.object(ctypes, "WinDLL", lambda *a, **k: funzioni, create=True):
            api = volume_info._ApiKernel32()
        with mock.patch.object(volume_info.shutil, "disk_usage", return_value=USO_BASE) as disk_usage:
            self.assertEqual(api.spazio("C:\\"), USO_BASE)
        disk_usage.assert_called_once_with("C:\\")

    @unittest.skipUnless(WINDOWS, "error mode reale solo su Windows")
    def test_error_mode_reale_impostato_e_ripristinato(self):
        api = volume_info._ApiKernel32()
        precedente = api.imposta_modalita_errori(SEM_FAILCRITICALERRORS)
        self.assertIsNotNone(precedente)
        try:
            self.assertEqual(api.imposta_modalita_errori(SEM_FAILCRITICALERRORS), SEM_FAILCRITICALERRORS)
        finally:
            api.imposta_modalita_errori(precedente)


# =====================================================================
# Unico smoke reale (Windows): nessuna lettera o capacità attesa
# =====================================================================

@unittest.skipUnless(WINDOWS, "backend unità locali disponibile solo su Windows")
class TestSmokeRealeVolumi(unittest.TestCase):

    def test_lettura_reale_invarianti(self):
        risultato = volume_info.leggi_volumi_locali()

        self.assertIsInstance(risultato, RisultatoVolumi)
        self.assertIs(type(risultato.totale), int)
        self.assertLessEqual(len(risultato.volumi), volume_info.MAX_LOCAL_VOLUMES)
        self.assertEqual(risultato.troncato, risultato.totale > volume_info.MAX_LOCAL_VOLUMES)
        self.assertEqual(len(risultato.volumi), min(risultato.totale, volume_info.MAX_LOCAL_VOLUMES))
        for volume in risultato.volumi:
            self.assertEqual(set(volume), CAMPI_VOLUME)
            self.assertIsNotNone(re.fullmatch(r"[A-Z]:", volume["drive"]))
            if volume["info_available"]:
                self.assertGreater(volume["total_bytes"], 0)
                self.assertEqual(volume["used_bytes"] + volume["free_bytes"], volume["total_bytes"])
                self.assertEqual(
                    volume["used_percent"],
                    volume["used_bytes"] * 100 // volume["total_bytes"],
                )
        json.dumps(risultato.volumi, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
