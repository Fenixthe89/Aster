"""
Aster 0.7.1a - Runtime Paths Foundation: test di modules/runtime_paths.py
e del controllo legacy files.* in modules/config.py.

Le simulazioni Windows/Linux/macOS usano solo la parte pura del
resolver (risolvi_percorsi, su PureWindowsPath/PurePosixPath): girano
identiche su qualsiasi host. I test sul filesystem reale usano
esclusivamente directory tempfile. Nessun file in data/ viene mai
letto o scritto; config.json reale non viene mai letto.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import builtins
import dataclasses
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from modules import runtime_paths
from modules.config import _path_legacy_equivalente, carica_config
from modules.runtime_paths import (
    MODALITA_NORMALE,
    MODALITA_PORTABLE,
    MODALITA_SORGENTE,
    ErrorePercorsiRuntime,
    PercorsiRuntime,
    percorsi_runtime,
    risolvi_percorsi,
)

# Installazioni simulate (nessuna esiste davvero: la parte pura non
# tocca mai il filesystem).
MODULO_LINUX = "/opt/aster/modules/runtime_paths.py"
MODULO_WINDOWS = r"C:\Progetti\Aster\modules\runtime_paths.py"

EXE_WINDOWS = r"C:\Apps\Aster\Aster.exe"
MEIPASS_ONEFILE_WINDOWS = r"C:\Users\utente\AppData\Local\Temp\_MEI12345"
MEIPASS_ONEDIR_WINDOWS = r"C:\Apps\Aster\_internal"
HOME_WINDOWS = r"C:\Users\utente"
LOCALAPPDATA_WINDOWS = r"C:\Users\utente\AppData\Local"

EXE_LINUX = "/opt/aster/Aster"
MEIPASS_ONEFILE_LINUX = "/tmp/_MEI12345"
HOME_LINUX = "/home/utente"

EXE_MAC = "/Applications/Aster/Aster"
HOME_MAC = "/Users/utente"


def _marker_assente(percorso):
    return False


def _marker_mai_osservato(percorso):
    raise AssertionError("il marker portable non deve essere osservato")


class _SpiaMarker:
    """Registra i path osservati e risponde con un valore fisso."""

    def __init__(self, presente: bool):
        self.presente = presente
        self.chiamate = []

    def __call__(self, percorso):
        self.chiamate.append(percorso)
        return self.presente


def _risolvi_frozen(sistema, eseguibile, meipass=None, environ=None,
                    home=None, marker=_marker_assente):
    return risolvi_percorsi(
        frozen=True,
        eseguibile=eseguibile,
        meipass=meipass,
        file_modulo=None,
        sistema=sistema,
        environ=environ or {},
        home=home,
        marker_presente=marker,
    )


def _risolvi_sorgente(sistema, file_modulo, marker=_marker_mai_osservato):
    return risolvi_percorsi(
        frozen=False,
        eseguibile=None,
        meipass=None,
        file_modulo=file_modulo,
        sistema=sistema,
        environ={},
        home=None,
        marker_presente=marker,
    )


def _windows(environ=None, home=HOME_WINDOWS):
    return _risolvi_frozen("win32", EXE_WINDOWS, MEIPASS_ONEFILE_WINDOWS,
                           environ, home)


def _linux(environ=None, home=HOME_LINUX, sistema="linux"):
    return _risolvi_frozen(sistema, EXE_LINUX, MEIPASS_ONEFILE_LINUX,
                           environ, home)


def _mac(environ=None, home=HOME_MAC):
    return _risolvi_frozen("darwin", EXE_MAC, None, environ, home)


def _elenco_albero(radice: Path) -> set:
    return {str(p.relative_to(radice)) for p in radice.rglob("*")}


# =====================================================================
# SOURCE
# =====================================================================


class TestModalitaSorgente(unittest.TestCase):

    def test_app_root_e_la_radice_del_repository(self):
        percorsi = _risolvi_sorgente("linux", MODULO_LINUX)
        self.assertEqual(percorsi.modalita, MODALITA_SORGENTE)
        self.assertEqual(percorsi.app_root, PurePosixPath("/opt/aster"))

    def test_resource_root_uguale_app_root(self):
        percorsi = _risolvi_sorgente("linux", MODULO_LINUX)
        self.assertEqual(percorsi.resource_root, percorsi.app_root)

    def test_data_root_e_app_root_data(self):
        percorsi = _risolvi_sorgente("linux", MODULO_LINUX)
        self.assertEqual(percorsi.data_root, percorsi.app_root / "data")

    def test_sorgente_windows_simulato(self):
        percorsi = _risolvi_sorgente("win32", MODULO_WINDOWS)
        self.assertIsInstance(percorsi.app_root, PureWindowsPath)
        self.assertEqual(percorsi.app_root, PureWindowsPath(r"C:\Progetti\Aster"))
        self.assertEqual(percorsi.data_root, PureWindowsPath(r"C:\Progetti\Aster\data"))

    def test_path_equivalenti_alla_v068(self):
        # v0.6.8: BASE_DIR = cartella di aster.py (radice progetto),
        # CONFIG_FILE = BASE_DIR/"config.json", prompt e memoria da
        # config["files"] con i valori legacy.
        for sistema, modulo, classe in (
            ("linux", MODULO_LINUX, PurePosixPath),
            ("win32", MODULO_WINDOWS, PureWindowsPath),
        ):
            with self.subTest(sistema=sistema):
                aster_py = classe(modulo).parent.parent / "aster.py"
                base_legacy = aster_py.parent
                percorsi = _risolvi_sorgente(sistema, modulo)

                self.assertEqual(percorsi.config_file, base_legacy / "config.json")
                self.assertEqual(percorsi.prompt_file, base_legacy / "prompt.txt")
                self.assertEqual(percorsi.memory_file, base_legacy / "data/memory.json")

    def test_cestino_derivato_da_memory_file_resta_in_data_root(self):
        # memory_tools deriva deleted_memories.json dal parent di memory_file.
        percorsi = _risolvi_sorgente("linux", MODULO_LINUX)
        self.assertEqual(
            percorsi.memory_file.with_name("deleted_memories.json"),
            percorsi.data_root / "deleted_memories.json",
        )

    def test_marker_ignorato_e_mai_osservato_da_sorgente(self):
        spia = _SpiaMarker(presente=True)
        percorsi = _risolvi_sorgente("linux", MODULO_LINUX, marker=spia)
        self.assertEqual(percorsi.modalita, MODALITA_SORGENTE)
        self.assertEqual(percorsi.data_root, PurePosixPath("/opt/aster/data"))
        self.assertEqual(spia.chiamate, [])

    def test_file_modulo_mancante_o_relativo_fail_closed(self):
        for valore in (None, "", "modules/runtime_paths.py"):
            with self.subTest(valore=valore):
                with self.assertRaises(ErrorePercorsiRuntime):
                    _risolvi_sorgente("linux", valore)

    def test_runtime_reale_del_repository_identico_alla_v068(self):
        percorsi = percorsi_runtime()

        self.assertEqual(percorsi.modalita, MODALITA_SORGENTE)
        self.assertEqual(percorsi.app_root, BASE_DIR)
        self.assertEqual(percorsi.resource_root, BASE_DIR)
        self.assertEqual(percorsi.data_root, BASE_DIR / "data")
        self.assertEqual(percorsi.config_file, BASE_DIR / "config.json")
        self.assertEqual(percorsi.prompt_file, BASE_DIR / "prompt.txt")
        self.assertEqual(percorsi.memory_file, BASE_DIR / "data/memory.json")

    def test_runtime_reale_produce_veri_path(self):
        percorsi = percorsi_runtime()
        for radice in (percorsi.app_root, percorsi.resource_root, percorsi.data_root):
            self.assertIsInstance(radice, Path)
            self.assertTrue(radice.is_absolute())


# =====================================================================
# WINDOWS NORMAL (simulato)
# =====================================================================


class TestWindowsNormale(unittest.TestCase):

    def test_localappdata_assoluta(self):
        percorsi = _windows({"LOCALAPPDATA": LOCALAPPDATA_WINDOWS})
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)
        self.assertIsInstance(percorsi.data_root, PureWindowsPath)
        self.assertEqual(
            percorsi.data_root,
            PureWindowsPath(r"C:\Users\utente\AppData\Local\Aster"),
        )

    def test_localappdata_assente_fallback_home(self):
        percorsi = _windows({}, home=r"D:\Profili\utente")
        self.assertEqual(
            percorsi.data_root,
            PureWindowsPath(r"D:\Profili\utente\AppData\Local\Aster"),
        )

    def test_localappdata_vuota_fallback_home(self):
        percorsi = _windows({"LOCALAPPDATA": ""}, home=r"D:\Profili\utente")
        self.assertEqual(
            percorsi.data_root,
            PureWindowsPath(r"D:\Profili\utente\AppData\Local\Aster"),
        )

    def test_localappdata_relativa_o_non_valida_fallback_home(self):
        for valore in ("AppData\\Local", "\\AppData\\Local", "C:AppData",
                       "   ", "C:\\Users\\u\x00\\Local"):
            with self.subTest(valore=valore):
                percorsi = _windows({"LOCALAPPDATA": valore},
                                    home=r"D:\Profili\utente")
                self.assertEqual(
                    percorsi.data_root,
                    PureWindowsPath(r"D:\Profili\utente\AppData\Local\Aster"),
                )

    def test_nessuna_cwd_senza_localappdata_ne_home(self):
        for home in (None, "", "utente", "\\Users\\utente"):
            with self.subTest(home=home):
                with self.assertRaises(ErrorePercorsiRuntime):
                    _windows({"LOCALAPPDATA": "AppData"}, home=home)

    def test_data_root_sempre_assoluta(self):
        for environ in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "rel"},
                        {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS}):
            with self.subTest(environ=environ):
                self.assertTrue(_windows(environ).data_root.is_absolute())

    def test_xdg_ignorato_su_windows(self):
        percorsi = _windows({"XDG_DATA_HOME": r"C:\xdg"}, home=r"D:\Profili\u")
        self.assertEqual(
            percorsi.data_root,
            PureWindowsPath(r"D:\Profili\u\AppData\Local\Aster"),
        )


# =====================================================================
# LINUX / POSIX NORMAL (simulato)
# =====================================================================


class TestLinuxNormale(unittest.TestCase):

    def test_xdg_data_home_assoluta(self):
        percorsi = _linux({"XDG_DATA_HOME": "/dati/xdg"})
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)
        self.assertIsInstance(percorsi.data_root, PurePosixPath)
        self.assertEqual(percorsi.data_root, PurePosixPath("/dati/xdg/aster"))

    def test_xdg_data_home_relativa_ignorata(self):
        percorsi = _linux({"XDG_DATA_HOME": "dati/xdg"})
        self.assertEqual(
            percorsi.data_root,
            PurePosixPath("/home/utente/.local/share/aster"),
        )

    def test_xdg_data_home_assente_fallback(self):
        percorsi = _linux({})
        self.assertEqual(
            percorsi.data_root,
            PurePosixPath("/home/utente/.local/share/aster"),
        )

    def test_xdg_data_home_vuota_fallback(self):
        percorsi = _linux({"XDG_DATA_HOME": ""})
        self.assertEqual(
            percorsi.data_root,
            PurePosixPath("/home/utente/.local/share/aster"),
        )

    def test_xdg_valida_basta_anche_senza_home(self):
        percorsi = _linux({"XDG_DATA_HOME": "/dati/xdg"}, home=None)
        self.assertEqual(percorsi.data_root, PurePosixPath("/dati/xdg/aster"))

    def test_posix_sconosciuto_usa_regole_posix(self):
        percorsi = _linux({}, sistema="freebsd14")
        self.assertEqual(
            percorsi.data_root,
            PurePosixPath("/home/utente/.local/share/aster"),
        )
        percorsi = _linux({"XDG_DATA_HOME": "/dati/xdg"}, sistema="freebsd14")
        self.assertEqual(percorsi.data_root, PurePosixPath("/dati/xdg/aster"))


# =====================================================================
# MACOS NORMAL (simulato)
# =====================================================================


class TestMacNormale(unittest.TestCase):

    def test_application_support(self):
        percorsi = _mac({})
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)
        self.assertEqual(
            percorsi.data_root,
            PurePosixPath("/Users/utente/Library/Application Support/Aster"),
        )

    def test_xdg_ignorato(self):
        percorsi = _mac({"XDG_DATA_HOME": "/dati/xdg"})
        self.assertEqual(
            percorsi.data_root,
            PurePosixPath("/Users/utente/Library/Application Support/Aster"),
        )

    def test_xdg_non_sostituisce_una_home_mancante(self):
        with self.assertRaises(ErrorePercorsiRuntime):
            _mac({"XDG_DATA_HOME": "/dati/xdg"}, home=None)


# =====================================================================
# HOME
# =====================================================================


class TestHomeFailClosed(unittest.TestCase):

    def test_home_mancante_o_non_valida_errore_su_ogni_sistema(self):
        casi = (
            ("win32", EXE_WINDOWS),
            ("linux", EXE_LINUX),
            ("darwin", EXE_MAC),
            ("freebsd14", EXE_LINUX),
        )
        for sistema, eseguibile in casi:
            for home in (None, "", "utente", "~"):
                with self.subTest(sistema=sistema, home=home):
                    with self.assertRaises(ErrorePercorsiRuntime):
                        _risolvi_frozen(sistema, eseguibile, None, {}, home)

    def test_errore_non_espone_valori_ricevuti(self):
        with self.assertRaises(ErrorePercorsiRuntime) as contesto:
            _linux({"XDG_DATA_HOME": "XDG_RELATIVA_X1"}, home="HOME_RELATIVA_X2")
        messaggio = str(contesto.exception)
        self.assertNotIn("XDG_RELATIVA_X1", messaggio)
        self.assertNotIn("HOME_RELATIVA_X2", messaggio)


# =====================================================================
# FROZEN (simulato)
# =====================================================================


class TestFrozen(unittest.TestCase):

    def test_app_root_e_la_directory_dell_eseguibile(self):
        percorsi = _windows({"LOCALAPPDATA": LOCALAPPDATA_WINDOWS})
        self.assertEqual(percorsi.app_root, PureWindowsPath(r"C:\Apps\Aster"))

    def test_resource_root_e_meipass(self):
        percorsi = _windows({"LOCALAPPDATA": LOCALAPPDATA_WINDOWS})
        self.assertEqual(percorsi.resource_root, PureWindowsPath(MEIPASS_ONEFILE_WINDOWS))
        self.assertEqual(percorsi.prompt_file,
                         PureWindowsPath(MEIPASS_ONEFILE_WINDOWS) / "prompt.txt")

    def test_meipass_mancante_resource_root_uguale_app_root(self):
        for meipass in (None, ""):
            with self.subTest(meipass=meipass):
                percorsi = _risolvi_frozen("win32", EXE_WINDOWS, meipass,
                                           {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS})
                self.assertEqual(percorsi.resource_root, percorsi.app_root)

    def test_meipass_relativo_fail_closed(self):
        with self.assertRaises(ErrorePercorsiRuntime):
            _risolvi_frozen("win32", EXE_WINDOWS, "_MEI12345",
                            {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS})

    def test_eseguibile_mancante_o_relativo_fail_closed(self):
        for eseguibile in (None, "", "Aster.exe"):
            with self.subTest(eseguibile=eseguibile):
                with self.assertRaises(ErrorePercorsiRuntime):
                    _risolvi_frozen("win32", eseguibile, MEIPASS_ONEFILE_WINDOWS,
                                    {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS})

    def test_data_root_mai_dentro_meipass(self):
        casi = (
            ("win32", EXE_WINDOWS, MEIPASS_ONEFILE_WINDOWS,
             {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS}, HOME_WINDOWS),
            ("win32", EXE_WINDOWS, MEIPASS_ONEDIR_WINDOWS, {}, HOME_WINDOWS),
            ("linux", EXE_LINUX, MEIPASS_ONEFILE_LINUX, {}, HOME_LINUX),
            ("darwin", EXE_MAC, "/private/var/folders/x/_MEI1", {}, HOME_MAC),
        )
        for sistema, eseguibile, meipass, environ, home in casi:
            for presente in (False, True):
                with self.subTest(sistema=sistema, meipass=meipass, portable=presente):
                    percorsi = _risolvi_frozen(
                        sistema, eseguibile, meipass, environ, home,
                        marker=_SpiaMarker(presente),
                    )
                    self.assertFalse(percorsi.data_root.is_relative_to(meipass))
                    self.assertFalse(
                        percorsi.memory_file.is_relative_to(percorsi.resource_root)
                    )


# =====================================================================
# PORTABLE
# =====================================================================


class TestPortable(unittest.TestCase):

    def test_marker_frozen_data_root_app_root_data(self):
        spia = _SpiaMarker(presente=True)
        percorsi = _risolvi_frozen("win32", EXE_WINDOWS, MEIPASS_ONEFILE_WINDOWS,
                                   {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS},
                                   HOME_WINDOWS, marker=spia)
        self.assertEqual(percorsi.modalita, MODALITA_PORTABLE)
        self.assertEqual(percorsi.data_root, PureWindowsPath(r"C:\Apps\Aster\data"))
        self.assertEqual(percorsi.resource_root, PureWindowsPath(MEIPASS_ONEFILE_WINDOWS))

    def test_marker_cercato_solo_in_app_root(self):
        spia = _SpiaMarker(presente=False)
        _risolvi_frozen("win32", EXE_WINDOWS, MEIPASS_ONEFILE_WINDOWS,
                        {"LOCALAPPDATA": LOCALAPPDATA_WINDOWS}, HOME_WINDOWS,
                        marker=spia)
        self.assertEqual(spia.chiamate, [PureWindowsPath(r"C:\Apps\Aster\portable.txt")])

    def test_marker_assente_data_root_normale(self):
        percorsi = _risolvi_frozen("linux", EXE_LINUX, MEIPASS_ONEFILE_LINUX,
                                   {}, HOME_LINUX, marker=_SpiaMarker(False))
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)
        self.assertEqual(percorsi.data_root,
                         PurePosixPath("/home/utente/.local/share/aster"))

    def test_portable_non_richiede_home(self):
        percorsi = _risolvi_frozen("linux", EXE_LINUX, None, {}, None,
                                   marker=_SpiaMarker(True))
        self.assertEqual(percorsi.data_root, PurePosixPath("/opt/aster/data"))

    def test_source_resta_repo_data_anche_con_marker(self):
        percorsi = _risolvi_sorgente("linux", MODULO_LINUX, marker=_SpiaMarker(True))
        self.assertEqual(percorsi.modalita, MODALITA_SORGENTE)
        self.assertEqual(percorsi.data_root, PurePosixPath("/opt/aster/data"))


class TestMarkerSuFilesystemReale(unittest.TestCase):
    """Verifica reale del marker su directory tempfile dell'host corrente."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.app = self.tmp / "app"
        self.app.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _risolvi_host(self):
        return risolvi_percorsi(
            frozen=True,
            eseguibile=str(self.app / "Aster.exe"),
            meipass=str(self.tmp / "_MEI1"),
            file_modulo=None,
            sistema=sys.platform,
            environ={
                "LOCALAPPDATA": str(self.tmp / "local"),
                "XDG_DATA_HOME": str(self.tmp / "xdg"),
            },
            home=str(self.tmp / "home"),
            marker_presente=runtime_paths._file_marker_esiste,
        )

    def test_file_portable_txt_attiva_portable(self):
        (self.app / "portable.txt").write_text("", encoding="utf-8")
        percorsi = self._risolvi_host()
        self.assertEqual(percorsi.modalita, MODALITA_PORTABLE)
        self.assertEqual(Path(percorsi.data_root), self.app / "data")

    def test_directory_chiamata_portable_txt_non_vale_come_marker(self):
        (self.app / "portable.txt").mkdir()
        percorsi = self._risolvi_host()
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)

    def test_marker_assente_modalita_normale(self):
        percorsi = self._risolvi_host()
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)
        self.assertTrue(Path(percorsi.data_root).is_relative_to(self.tmp))

    def test_marker_fuori_da_app_root_ignorato(self):
        (self.tmp / "portable.txt").write_text("", encoding="utf-8")
        percorsi = self._risolvi_host()
        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)


# =====================================================================
# CWD
# =====================================================================


class TestIndipendenzaDallaCwd(unittest.TestCase):

    def test_resolver_identico_dopo_chdir(self):
        prima_runtime = percorsi_runtime()
        prima_puro = _linux({"XDG_DATA_HOME": "rel"})
        cwd_originale = os.getcwd()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                dopo_runtime = percorsi_runtime()
                dopo_puro = _linux({"XDG_DATA_HOME": "rel"})
            finally:
                os.chdir(cwd_originale)

        self.assertEqual(prima_runtime, dopo_runtime)
        self.assertEqual(prima_puro, dopo_puro)
        self.assertFalse(Path(tmp) in Path(dopo_runtime.data_root).parents)


# =====================================================================
# SIDE EFFECT
# =====================================================================


class TestNessunSideEffect(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolver_puro_non_crea_nulla(self):
        (self.tmp / "app" / "modules").mkdir(parents=True)
        prima = _elenco_albero(self.tmp)

        for frozen, presente in ((False, False), (True, False), (True, True)):
            with self.subTest(frozen=frozen, portable=presente):
                percorsi = risolvi_percorsi(
                    frozen=frozen,
                    eseguibile=str(self.tmp / "app" / "Aster.exe"),
                    meipass=str(self.tmp / "_MEI1"),
                    file_modulo=str(self.tmp / "app" / "modules" / "runtime_paths.py"),
                    sistema=sys.platform,
                    environ={
                        "LOCALAPPDATA": str(self.tmp / "local"),
                        "XDG_DATA_HOME": str(self.tmp / "xdg"),
                    },
                    home=str(self.tmp / "home"),
                    marker_presente=_SpiaMarker(presente),
                )
                self.assertFalse(Path(percorsi.data_root).exists())

        self.assertEqual(_elenco_albero(self.tmp), prima)

    def _vieta_scritture(self):
        def vietato(*args, **kwargs):
            raise AssertionError("il resolver non deve scrivere")

        return [
            mock.patch.object(builtins, "open", vietato),
            mock.patch.object(os, "open", vietato),
            mock.patch.object(os, "mkdir", vietato),
            mock.patch.object(os, "makedirs", vietato),
            mock.patch.object(os, "replace", vietato),
            mock.patch.object(Path, "mkdir", vietato),
            mock.patch.object(Path, "touch", vietato),
        ]

    def test_percorsi_runtime_sorgente_nessuna_scrittura(self):
        patch_attive = self._vieta_scritture()
        for patch in patch_attive:
            patch.start()
        try:
            percorsi_runtime()
        finally:
            for patch in reversed(patch_attive):
                patch.stop()

    def test_percorsi_runtime_frozen_simulato_veri_path_senza_scritture(self):
        app = self.tmp / "app"
        app.mkdir()
        prima = _elenco_albero(self.tmp)

        patch_attive = [
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(app / "Aster.exe")),
            mock.patch.object(sys, "_MEIPASS", str(self.tmp / "_MEI1"), create=True),
            mock.patch.object(runtime_paths, "_home_corrente",
                              lambda: str(self.tmp / "home")),
            mock.patch.dict(os.environ, {
                "LOCALAPPDATA": str(self.tmp / "local"),
                "XDG_DATA_HOME": str(self.tmp / "xdg"),
            }),
            *self._vieta_scritture(),
        ]
        for patch in patch_attive:
            patch.start()
        try:
            percorsi = percorsi_runtime()
        finally:
            for patch in reversed(patch_attive):
                patch.stop()

        self.assertEqual(percorsi.modalita, MODALITA_NORMALE)
        self.assertEqual(percorsi.app_root, app)
        self.assertEqual(percorsi.resource_root, self.tmp / "_MEI1")
        for radice in (percorsi.app_root, percorsi.resource_root, percorsi.data_root):
            self.assertIsInstance(radice, Path)
        self.assertTrue(percorsi.data_root.is_relative_to(self.tmp))
        self.assertFalse(percorsi.data_root.is_relative_to(self.tmp / "_MEI1"))
        self.assertEqual(_elenco_albero(self.tmp), prima)

    def test_percorsi_runtime_frozen_eseguibile_vuoto_fail_closed(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", ""):
            with self.assertRaises(ErrorePercorsiRuntime):
                percorsi_runtime()

    def test_percorsi_runtime_frozen_eseguibile_relativo_fail_closed(self):
        # Il wrapper reale non deve risolvere un sys.executable relativo
        # rispetto alla cwd prima della validazione.
        for eseguibile in ("Aster.exe", os.path.join("dist", "Aster.exe"), None):
            with self.subTest(eseguibile=eseguibile):
                with mock.patch.object(sys, "frozen", True, create=True), \
                        mock.patch.object(sys, "executable", eseguibile), \
                        mock.patch.object(sys, "_MEIPASS", str(self.tmp / "_MEI1"),
                                          create=True):
                    with self.assertRaises(ErrorePercorsiRuntime):
                        percorsi_runtime()

    def test_percorsi_runtime_frozen_meipass_relativo_fail_closed(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable",
                                  str(self.tmp / "app" / "Aster.exe")), \
                mock.patch.object(sys, "_MEIPASS", "_MEI12345", create=True):
            with self.assertRaises(ErrorePercorsiRuntime):
                percorsi_runtime()


# =====================================================================
# LEGACY files.*
# =====================================================================


def _config_con_files(files) -> dict:
    config = {
        "assistant": {"name": "Aster", "version": "0.7.1"},
        "ollama": {"model": "qwen3:8b", "host": "http://localhost:11434"},
        "chat": {"history_limit": 20, "stream": True},
        "memory": {"search_max_results": 5},
    }
    if files is not None:
        config["files"] = files
    return config


class TestFilesLegacy(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _carica(self, files):
        percorso = self.tmp / "config.json"
        percorso.write_text(json.dumps(_config_con_files(files)), encoding="utf-8")
        return carica_config(percorso)

    def test_valori_default_accettati(self):
        config = self._carica({"prompt": "prompt.txt", "memory": "data/memory.json"})
        self.assertEqual(config["files"]["memory"], "data/memory.json")

    def test_sezione_o_chiavi_assenti_accettate(self):
        self._carica(None)
        self._carica({})
        self._carica({"prompt": "prompt.txt"})

    def test_equivalenti_legacy_accettati(self):
        for prompt, memoria in (
            ("./prompt.txt", "./data/memory.json"),
            ("prompt.txt", "data//memory.json"),
            ("prompt.txt", "data/./memory.json"),
        ):
            with self.subTest(prompt=prompt, memoria=memoria):
                self._carica({"prompt": prompt, "memory": memoria})

    def test_prompt_personalizzato_rifiutato(self):
        with self.assertRaises(ValueError) as contesto:
            self._carica({"prompt": "persona.txt", "memory": "data/memory.json"})
        self.assertIn("files.prompt", str(contesto.exception))
        self.assertNotIn("persona.txt", str(contesto.exception))

    def test_memory_personalizzata_rifiutata(self):
        for memoria in ("archivio/memory.json", "data/altro.json", "memory.json"):
            with self.subTest(memoria=memoria):
                with self.assertRaises(ValueError) as contesto:
                    self._carica({"prompt": "prompt.txt", "memory": memoria})
                self.assertIn("files.memory", str(contesto.exception))

    def test_path_assoluti_rifiutati(self):
        assoluti = (
            str((self.tmp / "data" / "memory.json").resolve()),
            "/data/memory.json",
            "C:\\Aster\\data\\memory.json",
            "\\data\\memory.json",
            "C:data\\memory.json",
        )
        for valore in assoluti:
            with self.subTest(valore=valore):
                with self.assertRaises(ValueError):
                    self._carica({"prompt": "prompt.txt", "memory": valore})
                with self.assertRaises(ValueError):
                    self._carica({"prompt": valore, "memory": "data/memory.json"})

    def test_traversal_rifiutato(self):
        for valore in ("../data/memory.json", "data/../data/memory.json",
                       "..\\data\\memory.json"):
            with self.subTest(valore=valore):
                with self.assertRaises(ValueError):
                    self._carica({"prompt": "prompt.txt", "memory": valore})
        with self.assertRaises(ValueError):
            self._carica({"prompt": "../prompt.txt", "memory": "data/memory.json"})

    def test_tipi_non_validi_rifiutati(self):
        with self.assertRaises(TypeError):
            self._carica(["prompt.txt"])
        with self.assertRaises(TypeError):
            self._carica({"prompt": None, "memory": "data/memory.json"})
        with self.assertRaises(TypeError):
            self._carica({"prompt": "prompt.txt", "memory": 42})

    def test_valori_vuoti_rifiutati(self):
        with self.assertRaises(ValueError):
            self._carica({"prompt": "", "memory": "data/memory.json"})

    def test_equivalenza_secondo_le_regole_del_sistema(self):
        attese = ("data", "memory.json")
        # Windows: separatore "\" e confronto case-insensitive come il
        # filesystem su cui il vecchio codice risolveva lo stesso file.
        self.assertTrue(_path_legacy_equivalente("data\\memory.json", attese, PureWindowsPath))
        self.assertTrue(_path_legacy_equivalente("Data\\Memory.JSON", attese, PureWindowsPath))
        # POSIX: "\" è un carattere del nome, non un separatore.
        self.assertFalse(_path_legacy_equivalente("data\\memory.json", attese, PurePosixPath))
        self.assertTrue(_path_legacy_equivalente("./data/memory.json", attese, PurePosixPath))


# =====================================================================
# SECURITY: i path runtime non raggiungono tool, risultati o prompt
# =====================================================================


class TestConfinamentoRuntimePaths(unittest.TestCase):

    def test_solo_chat_importa_runtime_paths_tra_i_moduli(self):
        # Nessun handler tool (system/filesystem/memory) vede le radici
        # runtime: non possono quindi finire in un risultato role=tool.
        import_runtime_paths = re.compile(
            r"^\s*(from\s+modules\.runtime_paths\s+import"
            r"|from\s+modules\s+import\s+.*\bruntime_paths\b"
            r"|import\s+modules\.runtime_paths)",
            re.MULTILINE,
        )
        importatori = set()
        for sorgente in (BASE_DIR / "modules").glob("*.py"):
            if import_runtime_paths.search(sorgente.read_text(encoding="utf-8")):
                importatori.add(sorgente.name)
        self.assertEqual(importatori, {"chat.py"})

    def test_nessuna_scrittura_nel_modulo(self):
        sorgente = (BASE_DIR / "modules" / "runtime_paths.py").read_text(encoding="utf-8")
        for vietato in ("mkdir", "write_", "open(", "os.replace", "rename",
                        "unlink", "rmtree", "getcwd", "Path.cwd", "subprocess"):
            with self.subTest(vietato=vietato):
                self.assertNotIn(vietato, sorgente)

    def test_percorsi_runtime_e_immutabile(self):
        percorsi = percorsi_runtime()
        self.assertIsInstance(percorsi, PercorsiRuntime)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            percorsi.data_root = Path("altrove")


if __name__ == "__main__":
    unittest.main()
