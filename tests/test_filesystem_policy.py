"""
0.6.5 - Filesystem Safety Foundation: test di regressione.

Verifica modules/filesystem_policy.py: policy DEFAULT DENY per
containment filesystem, usata in futuro da list_directory/read_file
(non ancora implementati). Copre validazione delle root, containment
dopo resolve() (mai ricerca testuale di ".."), path assoluti e
relativi, ambiguita' multi-root, traversal, symlink (skip pulito se
la piattaforma non li supporta), privacy dei path negati, robustezza
su input non validi.

Usa esclusivamente tempfile/scratch: nessun file in data/ viene mai
letto o scritto. Nessuna chiamata a Ollama.

0.7.1c - Reserved Data Root: reserved_roots (keyword-only,
obbligatorio) prevale sulle allowed roots, con confronto dei path e
dell'identita' filesystem (st_dev, st_ino) e fail closed.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import inspect
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.filesystem_policy as filesystem_policy
from modules.filesystem_policy import (
    RAGIONI_AMMESSE,
    PathDecision,
    risolvi_path_autorizzato,
)


def _symlink_supportato(sorgente: Path, link: Path) -> bool:
    """Prova a creare un symlink di directory; True se riuscito."""

    try:
        link.symlink_to(sorgente, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


class FilesystemPolicyTestCase(unittest.TestCase):
    """Base comune: directory temporanea isolata per ogni test."""

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix="aster_test_fs_policy_"))

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)


# =====================================================================
# DEFAULT
# =====================================================================

class TestDefault(FilesystemPolicyTestCase):

    def test_allowed_roots_vuota_deny(self):
        decisione = risolvi_path_autorizzato("file.txt", [], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "no_allowed_roots")

    def test_tutte_root_invalide_deny(self):
        inesistente = self.base_dir / "non_esiste"
        decisione = risolvi_path_autorizzato(
            "file.txt", [str(inesistente), "relativo", ""], reserved_roots=()
        )

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "invalid_allowed_root")


# =====================================================================
# ROOT VALIDATION
# =====================================================================

class TestRootValidation(FilesystemPolicyTestCase):

    def test_root_assoluta_esistente_directory_valida(self):
        decisione = risolvi_path_autorizzato("file.txt", [self.base_dir], reserved_roots=())

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.reason, "ok")

    def test_root_inesistente_non_concede_accesso(self):
        inesistente = self.base_dir / "non_esiste_davvero"
        decisione = risolvi_path_autorizzato("file.txt", [inesistente], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_relativa_non_concede_accesso(self):
        decisione = risolvi_path_autorizzato("file.txt", ["workspace_relativo"], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_stringa_vuota_non_concede_accesso(self):
        decisione = risolvi_path_autorizzato("file.txt", [""], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_punto_non_concede_accesso(self):
        decisione = risolvi_path_autorizzato("file.txt", ["."], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_che_punta_a_file_non_concede_accesso(self):
        file_non_directory = self.base_dir / "sono_un_file.txt"
        file_non_directory.write_text("contenuto")

        decisione = risolvi_path_autorizzato("altro.txt", [file_non_directory], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_duplicate_dedup_corretto(self):
        # Due voci diverse che risolvono alla stessa root: deve
        # comportarsi come un'unica root valida (path relativo non
        # ambiguo).
        decisione = risolvi_path_autorizzato(
            "file.txt", [self.base_dir, str(self.base_dir)], reserved_roots=()
        )

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.reason, "ok")


# =====================================================================
# CONTAINMENT
# =====================================================================

class TestContainment(FilesystemPolicyTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.fuori = self.base_dir / "fuori"
        self.fuori.mkdir()

    def test_path_assoluto_dentro_root_allow(self):
        target = self.root / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root], reserved_roots=())

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.resolved_path, target.resolve())
        self.assertEqual(decisione.reason, "ok")

    def test_path_assoluto_fuori_root_deny(self):
        target = self.fuori / "segreto.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_sottocartella_dentro_root_allow(self):
        target = self.root / "sub" / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root], reserved_roots=())

        self.assertTrue(decisione.allowed)

    def test_traversal_che_resta_dentro_allow(self):
        (self.root / "sub").mkdir()
        target_str = str(self.root / "sub" / ".." / "altro.txt")

        decisione = risolvi_path_autorizzato(target_str, [self.root], reserved_roots=())

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.resolved_path, (self.root / "altro.txt").resolve())

    def test_traversal_che_esce_deny(self):
        target_str = str(self.root / ".." / "fuori" / "segreto.txt")

        decisione = risolvi_path_autorizzato(target_str, [self.root], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_target_inesistente_dentro_root_allow(self):
        target = self.root / "non_esiste_ancora" / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root], reserved_roots=())

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.reason, "ok")

    def test_target_inesistente_fuori_root_deny(self):
        target = self.fuori / "non_esiste" / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "outside_allowed_roots")


# =====================================================================
# RELATIVE
# =====================================================================

class TestPathRelativo(FilesystemPolicyTestCase):

    def setUp(self):
        super().setUp()
        self.root_a = self.base_dir / "root_a"
        self.root_a.mkdir()
        self.root_b = self.base_dir / "root_b"
        self.root_b.mkdir()

    def test_relativo_con_una_root_valida_allow_se_dentro(self):
        decisione = risolvi_path_autorizzato("sub/file.txt", [self.root_a], reserved_roots=())

        self.assertTrue(decisione.allowed)
        self.assertEqual(
            decisione.resolved_path, (self.root_a / "sub" / "file.txt").resolve()
        )

    def test_relativo_con_una_root_valida_deny_se_esce(self):
        decisione = risolvi_path_autorizzato("../fuori.txt", [self.root_a], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_relativo_con_piu_root_valide_ambiguo(self):
        decisione = risolvi_path_autorizzato(
            "sub/file.txt", [self.root_a, self.root_b], reserved_roots=()
        )

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "ambiguous_relative_path")


# =====================================================================
# MULTI ROOT
# =====================================================================

class TestMultiRoot(FilesystemPolicyTestCase):

    def setUp(self):
        super().setUp()
        self.root_a = self.base_dir / "root_a"
        self.root_a.mkdir()
        self.root_b = self.base_dir / "root_b"
        self.root_b.mkdir()
        self.fuori = self.base_dir / "fuori"
        self.fuori.mkdir()

    def test_assoluto_nella_prima_root_allow(self):
        target = self.root_a / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root_a, self.root_b], reserved_roots=())

        self.assertTrue(decisione.allowed)

    def test_assoluto_nella_seconda_root_allow(self):
        target = self.root_b / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root_a, self.root_b], reserved_roots=())

        self.assertTrue(decisione.allowed)

    def test_assoluto_fuori_da_entrambe_deny(self):
        target = self.fuori / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root_a, self.root_b], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "outside_allowed_roots")


# =====================================================================
# SYMLINK (skip pulito se la piattaforma non li supporta)
# =====================================================================

class TestSymlink(FilesystemPolicyTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()

    def test_symlink_interno_verso_esterno_deny(self):
        esterno = self.base_dir / "esterno"
        esterno.mkdir()
        (esterno / "segreto.txt").write_text("top secret")

        link = self.root / "link_esterno"
        if not _symlink_supportato(esterno, link):
            self.skipTest("symlink di directory non supportato su questa piattaforma")

        decisione = risolvi_path_autorizzato(
            str(self.root / "link_esterno" / "segreto.txt"), [self.root], reserved_roots=()
        )

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_symlink_interno_verso_interno_allow(self):
        interno = self.root / "interno"
        interno.mkdir()
        (interno / "file.txt").write_text("contenuto")

        link = self.root / "link_interno"
        if not _symlink_supportato(interno, link):
            self.skipTest("symlink di directory non supportato su questa piattaforma")

        decisione = risolvi_path_autorizzato(
            str(self.root / "link_interno" / "file.txt"), [self.root], reserved_roots=()
        )

        self.assertTrue(decisione.allowed)
        self.assertEqual(
            decisione.resolved_path, (interno / "file.txt").resolve()
        )


# =====================================================================
# PRIVACY
# =====================================================================

class TestPrivacy(FilesystemPolicyTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.base_dir / "root"
        self.root.mkdir()
        self.esterno = self.base_dir / "molto_esterno_e_personale"
        self.esterno.mkdir()

    def test_ogni_deny_ha_resolved_path_none(self):
        casi = [
            ([], "file.txt"),
            ([self.root], str(self.esterno / "segreto.txt")),
            ([self.root, self.root.parent / "altra"], "sub/file.txt"),
        ]

        for allowed_roots, path_richiesto in casi:
            with self.subTest(allowed_roots=allowed_roots, path_richiesto=path_richiesto):
                decisione = risolvi_path_autorizzato(path_richiesto, allowed_roots, reserved_roots=())
                if not decisione.allowed:
                    self.assertIsNone(decisione.resolved_path)

    def test_reason_non_contiene_path_esterno_richiesto(self):
        target_esterno = str(self.esterno / "informazione_privata.txt")
        decisione = risolvi_path_autorizzato(target_esterno, [self.root], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertNotIn("molto_esterno_e_personale", decisione.reason)
        self.assertNotIn("informazione_privata", decisione.reason)
        self.assertIn(decisione.reason, RAGIONI_AMMESSE)


class TestPathDecisionInvariante(unittest.TestCase):
    """L'invariante allowed=False -> resolved_path None e' garantito dal dataclass stesso."""

    def test_denied_con_resolved_path_non_none_solleva(self):
        with self.assertRaises(ValueError):
            PathDecision(
                allowed=False,
                resolved_path=Path("C:/qualcosa"),
                reason="outside_allowed_roots",
            )

    def test_allowed_senza_resolved_path_solleva(self):
        with self.assertRaises(ValueError):
            PathDecision(allowed=True, resolved_path=None, reason="ok")

    def test_reason_non_ammessa_solleva(self):
        with self.assertRaises(ValueError):
            PathDecision(allowed=False, resolved_path=None, reason="motivo_inventato")


# =====================================================================
# ROBUSTNESS
# =====================================================================

class TestRobustness(FilesystemPolicyTestCase):

    def test_path_richiesto_stringa_vuota_deny(self):
        decisione = risolvi_path_autorizzato("", [self.base_dir], reserved_roots=())

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "invalid_path")

    def test_path_richiesto_tipo_invalido_deny(self):
        for valore in (None, 123, ["a", "b"], {"x": 1}):
            with self.subTest(valore=valore):
                decisione = risolvi_path_autorizzato(valore, [self.base_dir], reserved_roots=())
                self.assertFalse(decisione.allowed)
                self.assertIsNone(decisione.resolved_path)
                self.assertEqual(decisione.reason, "invalid_path")

    def test_errore_resolve_simulato_non_propaga_eccezione(self):
        marcatore = "marcatore_rotto_di_prova"
        originale = pathlib.Path.resolve

        def resolve_selettivo(self_path, strict=False):
            if marcatore in str(self_path):
                raise OSError("errore di resolve simulato")
            return originale(self_path, strict=strict)

        pathlib.Path.resolve = resolve_selettivo
        try:
            decisione = risolvi_path_autorizzato(
                f"{marcatore}/file.txt", [self.base_dir], reserved_roots=()
            )
        finally:
            pathlib.Path.resolve = originale

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "path_resolution_error")

    def test_nessun_hardcode_utente_nel_sorgente(self):
        sorgente = (BASE_DIR / "modules" / "filesystem_policy.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("C:\\Aster", sorgente)
        self.assertNotIn("C:/Aster", sorgente)
        self.assertNotIn(str(Path.home()), sorgente)

    def test_nessuna_operazione_di_lettura_contenuto_nel_sorgente(self):
        sorgente = (BASE_DIR / "modules" / "filesystem_policy.py").read_text(
            encoding="utf-8"
        )
        for vietato in ("open(", ".listdir(", ".scandir(", ".glob(", ".read("):
            self.assertNotIn(vietato, sorgente, msg=vietato)

    def test_nessun_import_da_chat_memoria_o_registry(self):
        sorgente = (BASE_DIR / "modules" / "filesystem_policy.py").read_text(
            encoding="utf-8"
        )
        for vietato in (
            "modules.chat",
            "modules.memory",
            "modules.tool_registry",
            "modules.tool_response",
            "modules.system_tools",
            "modules.config",
        ):
            self.assertNotIn(vietato, sorgente, msg=vietato)


# =====================================================================
# RESERVED ROOTS (0.7.1c)
# =====================================================================


class ReservedTestCase(FilesystemPolicyTestCase):
    """Layout sorgente in tempfile: APP allowed, APP/data reserved."""

    def setUp(self):
        super().setUp()
        self.base = self.base_dir.resolve()
        self.app = self.base / "app"
        self.data = self.app / "data"
        self.data.mkdir(parents=True)
        (self.app / "prompt.txt").write_text("prompt")
        (self.data / "memory.json").write_text("{}")
        (self.data / "config.json").write_text("{}")

    _DATA = object()

    def _decidi(self, path, allowed=None, reserved=_DATA):
        # reserved=None e' un valore da testare (tipo errato), non
        # "assente": il default e' la sentinella _DATA -> (self.data,).
        return risolvi_path_autorizzato(
            str(path),
            [self.app] if allowed is None else allowed,
            reserved_roots=(self.data,) if reserved is ReservedTestCase._DATA else reserved,
        )

    def _assert_deny(self, decisione, reason="reserved_root"):
        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, reason)

    def _assert_allow(self, decisione, atteso=None):
        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.reason, "ok")
        if atteso is not None:
            self.assertEqual(decisione.resolved_path, Path(atteso).resolve())


class TestReservedCarveOut(ReservedTestCase):

    def test_sibling_allow(self):
        self._assert_allow(self._decidi(self.app / "prompt.txt"), self.app / "prompt.txt")

    def test_parent_della_reserved_allow(self):
        self._assert_allow(self._decidi(self.app), self.app)

    def test_reserved_stessa_deny(self):
        self._assert_deny(self._decidi(self.data))

    def test_child_reserved_deny(self):
        for target in (self.data / "memory.json", self.data / "config.json"):
            with self.subTest(target=target.name):
                self._assert_deny(self._decidi(target))

    def test_target_inesistente_dentro_reserved_deny(self):
        for target in (self.data / "inesistente.txt",
                       self.data / "nuova" / "cartella" / "file.txt"):
            with self.subTest(target=target.name):
                self._assert_deny(self._decidi(target))

    def test_path_relativi(self):
        self._assert_allow(self._decidi("prompt.txt"), self.app / "prompt.txt")
        for relativo in ("data", "data/memory.json", "data/inesistente.txt",
                         "./data/../data/config.json"):
            with self.subTest(relativo=relativo):
                self._assert_deny(self._decidi(relativo))

    def test_traversal_da_reserved_verso_fuori_allow(self):
        # Conta solo il path risolto finale, che e' fuori dalla reserved.
        atteso = self.app / "prompt.txt"
        self._assert_allow(self._decidi(self.app / "data" / ".." / "prompt.txt"), atteso)
        self._assert_allow(self._decidi("data/../prompt.txt"), atteso)

    def test_traversal_verso_reserved_deny(self):
        (self.app / "docs").mkdir()
        self._assert_deny(self._decidi(self.app / "docs" / ".." / "data" / "memory.json"))

    def test_allowed_contiene_reserved(self):
        docs = self.app / "docs"
        docs.mkdir()
        (docs / "nota.txt").write_text("x")

        self._assert_allow(self._decidi(docs / "nota.txt"), docs / "nota.txt")
        self._assert_deny(self._decidi(self.data / "memory.json"))

    def test_allowed_uguale_reserved(self):
        for target in (self.data, self.data / "memory.json", "memory.json", "."):
            with self.subTest(target=str(target)):
                self._assert_deny(self._decidi(target, allowed=[self.data]))

    def test_allowed_dentro_reserved(self):
        sub = self.data / "sub"
        sub.mkdir()
        for target in (sub, sub / "x.txt", "x.txt"):
            with self.subTest(target=str(target)):
                self._assert_deny(self._decidi(target, allowed=[sub]))

    def test_allowed_non_filtrate_ambiguita_relativa_invariata(self):
        # [app, data] restano due root valide: un relativo e' ambiguo
        # come prima, la reserved non ne elimina una.
        allowed = [self.app, self.data]
        self._assert_deny(self._decidi("prompt.txt", allowed=allowed),
                          "ambiguous_relative_path")
        self._assert_allow(self._decidi(self.app / "prompt.txt", allowed=allowed))
        self._assert_deny(self._decidi(self.data / "memory.json", allowed=allowed))

    def test_multiple_allowed_invariato(self):
        altra = self.base / "altra"
        altra.mkdir()
        (altra / "f.txt").write_text("x")
        allowed = [self.app, altra]

        self._assert_allow(self._decidi(altra / "f.txt", allowed=allowed), altra / "f.txt")
        self._assert_allow(self._decidi(self.app / "prompt.txt", allowed=allowed))
        self._assert_deny(self._decidi(self.data / "memory.json", allowed=allowed))
        self._assert_deny(self._decidi(self.base / "fuori.txt", allowed=allowed),
                          "outside_allowed_roots")

    def test_reason_esistenti_invariate_fuori_dalle_allowed(self):
        # Il veto arriva dopo il containment: un path riservato ma
        # esterno alle allowed resta outside_allowed_roots.
        altra = self.base / "altra"
        altra.mkdir()
        self._assert_deny(self._decidi(self.data / "memory.json", allowed=[altra]),
                          "outside_allowed_roots")
        self._assert_deny(self._decidi("x.txt", allowed=[]), "no_allowed_roots")

    def test_reserved_vuota_esplicita_comportamento_legacy(self):
        for reserved in ((), []):
            with self.subTest(reserved=reserved):
                self._assert_allow(self._decidi(self.data / "memory.json", reserved=reserved),
                                   self.data / "memory.json")

    def test_reserved_roots_keyword_only_obbligatorio(self):
        parametro = inspect.signature(risolvi_path_autorizzato).parameters["reserved_roots"]
        self.assertEqual(parametro.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parametro.default, inspect.Parameter.empty)

        with self.assertRaises(TypeError):
            risolvi_path_autorizzato("prompt.txt", [self.app])
        with self.assertRaises(TypeError):
            risolvi_path_autorizzato("prompt.txt", [self.app], (self.data,))

    def test_ogni_deny_reserved_senza_resolved_path(self):
        for target in (self.data, self.data / "memory.json", "data/x", self.data / "nuovo"):
            with self.subTest(target=str(target)):
                decisione = self._decidi(target)
                self.assertIsNone(decisione.resolved_path)
                self.assertIn(decisione.reason, RAGIONI_AMMESSE)
                self.assertNotIn(str(self.base), decisione.reason)


class TestReservedRicalcolataOgniDecisione(ReservedTestCase):

    def test_reserved_inesistente_protetta_dal_path(self):
        futura = self.app / "dati_futuri"
        reserved = (futura,)

        self._assert_deny(self._decidi(futura, reserved=reserved))
        self._assert_deny(self._decidi(futura / "memory.json", reserved=reserved))
        self._assert_allow(self._decidi(self.app / "prompt.txt", reserved=reserved))

    def test_reserved_inesistente_poi_creata(self):
        futura = self.app / "dati_futuri"
        target = futura / "memory.json"

        self._assert_deny(self._decidi(target, reserved=(futura,)))

        futura.mkdir()
        target.write_text("{}")
        self._assert_deny(self._decidi(target, reserved=(futura,)))

    def test_reserved_sotto_un_file_trattata_come_inesistente(self):
        # Un componente intermedio file: nulla puo' esistere li', ma il
        # path resta comunque riservato.
        reserved = (self.app / "prompt.txt" / "dati",)
        self._assert_deny(self._decidi(self.app / "prompt.txt" / "dati" / "x", reserved=reserved))
        self._assert_allow(self._decidi(self.app / "prompt.txt", reserved=reserved))

    def test_symlink_creato_dopo_la_preparazione(self):
        link = self.app / "alias"
        target = link / "memory.json"

        # Prima del link e' un path qualunque dentro app.
        self._assert_allow(self._decidi(target))

        if not _symlink_supportato(self.data, link):
            self.skipTest("symlink di directory non supportato su questa piattaforma")
        self._assert_deny(self._decidi(target))

    def test_reserved_symlink_cambiata_dopo(self):
        prima = self.base / "prima"
        dopo = self.base / "dopo"
        for directory in (prima, dopo):
            directory.mkdir()
            (directory / "x.txt").write_text("x")
        data_link = self.app / "data_link"
        if not _symlink_supportato(prima, data_link):
            self.skipTest("symlink di directory non supportato su questa piattaforma")

        allowed = [self.base]
        reserved = (data_link,)
        self._assert_deny(self._decidi(prima / "x.txt", allowed=allowed, reserved=reserved))
        self._assert_allow(self._decidi(dopo / "x.txt", allowed=allowed, reserved=reserved))

        os.unlink(data_link)
        data_link.symlink_to(dopo, target_is_directory=True)

        self._assert_deny(self._decidi(dopo / "x.txt", allowed=allowed, reserved=reserved))
        self._assert_allow(self._decidi(prima / "x.txt", allowed=allowed, reserved=reserved))


class TestReservedNonValida(ReservedTestCase):
    """Fail closed: una reserved non verificabile nega tutto."""

    def test_reserved_relativa_nega_tutto(self):
        for reserved in (("data",), ("",), (".",), (self.data, "relativa")):
            with self.subTest(reserved=reserved):
                self._assert_deny(self._decidi(self.app / "prompt.txt", reserved=reserved),
                                  "invalid_reserved_root")

    def test_reserved_tipo_errato_nega_tutto(self):
        for reserved in (None, 42, str(self.data), self.data, b"dati",
                         (None,), (42,), (b"dati",), ([str(self.data)],)):
            with self.subTest(reserved=repr(reserved)):
                self._assert_deny(self._decidi(self.app / "prompt.txt", reserved=reserved),
                                  "invalid_reserved_root")

    def test_errore_resolve_reserved_nega_tutto(self):
        resolve_originale = pathlib.Path.resolve

        def resolve_selettivo(percorso, *args, **kwargs):
            if percorso == self.data:
                raise OSError("resolve simulato")
            return resolve_originale(percorso, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "resolve", resolve_selettivo):
            decisione = self._decidi(self.app / "prompt.txt")

        self._assert_deny(decisione, "invalid_reserved_root")

    def _con_stat(self, bersaglio, sostituto):
        stat_originale = os.stat

        def stat_selettivo(percorso, *args, **kwargs):
            if isinstance(percorso, (str, os.PathLike)) and Path(percorso) == bersaglio:
                return sostituto(stat_originale(percorso, *args, **kwargs))
            return stat_originale(percorso, *args, **kwargs)

        return mock.patch.object(os, "stat", stat_selettivo)

    def test_stat_reserved_errore_nega_tutto(self):
        def negato(_):
            raise PermissionError(13, "accesso negato simulato")

        with self._con_stat(self.data, negato):
            decisione = self._decidi(self.app / "prompt.txt")

        self._assert_deny(decisione, "invalid_reserved_root")

    def test_identita_reserved_non_significativa_nega_tutto(self):
        # Reserved esistente con st_ino nullo: identita' non affidabile,
        # il controllo non viene saltato -> invalid_reserved_root.
        with self._con_stat(self.data, lambda st: SimpleNamespace(st_dev=st.st_dev, st_ino=0)):
            decisione = self._decidi(self.app / "prompt.txt")

        self._assert_deny(decisione, "invalid_reserved_root")

    def test_antenato_target_non_verificabile_nega(self):
        # Lato target: un antenato esistente non identificabile potrebbe
        # essere la reserved -> path_resolution_error.
        docs = self.app / "docs"
        docs.mkdir()
        (docs / "nota.txt").write_text("x")

        def negato(_):
            raise PermissionError(13, "accesso negato simulato")

        for sostituto in (negato, lambda st: SimpleNamespace(st_dev=st.st_dev, st_ino=0)):
            with self.subTest(sostituto=sostituto):
                with self._con_stat(docs, sostituto):
                    self._assert_deny(self._decidi(docs / "nota.txt"), "path_resolution_error")
                    # Con reserved_roots=() nessun controllo di identita':
                    # comportamento legacy invariato.
                    self._assert_allow(self._decidi(docs / "nota.txt", reserved=()))


class TestReservedSymlink(ReservedTestCase):

    def test_symlink_allowed_verso_reserved_deny(self):
        link = self.app / "alias"
        if not _symlink_supportato(self.data, link):
            self.skipTest("symlink di directory non supportato su questa piattaforma")

        for target in (link, link / "memory.json", link / "inesistente.txt"):
            with self.subTest(target=target.name):
                self._assert_deny(self._decidi(target))
        self._assert_deny(self._decidi("alias/memory.json"))
        self._assert_allow(self._decidi(self.app / "prompt.txt"))

    def test_symlink_file_verso_file_reserved_deny(self):
        link = self.app / "memoria_alias.json"
        try:
            link.symlink_to(self.data / "memory.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlink di file non supportato su questa piattaforma")

        self._assert_deny(self._decidi(link))

    def test_reserved_stessa_symlink_verso_altra_directory(self):
        # DATA_ROOT e' un symlink: protetti il path runtime e il target reale.
        reale = self.base / "altrove" / "dati_reali"
        reale.mkdir(parents=True)
        (reale / "memory.json").write_text("{}")
        (self.base / "altrove" / "vicino.txt").write_text("x")
        data_link = self.app / "data_link"
        if not _symlink_supportato(reale, data_link):
            self.skipTest("symlink di directory non supportato su questa piattaforma")

        allowed = [self.base]
        reserved = (data_link,)
        for target in (data_link, data_link / "memory.json", reale,
                       reale / "memory.json", reale / "nuovo.txt"):
            with self.subTest(target=str(target.relative_to(self.base))):
                self._assert_deny(self._decidi(target, allowed=allowed, reserved=reserved))
        self._assert_allow(self._decidi(self.base / "altrove" / "vicino.txt",
                                        allowed=allowed, reserved=reserved))

    @unittest.skipUnless(sys.platform == "win32", "junction solo su Windows")
    def test_junction_verso_reserved_deny(self):
        import _winapi

        giunzione = self.app / "giunzione"
        _winapi.CreateJunction(str(self.data), str(giunzione))

        for target in (giunzione, giunzione / "memory.json", giunzione / "nuovo.txt"):
            with self.subTest(target=target.name):
                self._assert_deny(self._decidi(target))


class TestReservedIdentita(ReservedTestCase):
    """
    Proprieta' verificata: nessuna rappresentazione alternativa di
    DATA_ROOT (\\\\?\\, UNC loopback, junction, symlink, alias con la
    stessa identita' filesystem) apre l'accesso alla zona riservata.

    Residuo NOTO della 0.7.1c, non comportamento desiderato: un hard
    link creato fuori da DATA_ROOT verso un singolo file interno non e'
    riconoscibile ne' dal path ne' dall'identita' della directory
    riservata. Crearlo richiede capacita' locale di scrittura/creazione
    link e Aster non ha write tools. Nessun test richiede che quel caso
    sia consentito: chiuderlo in futuro non sara' un breaking change.
    """

    def test_stessa_identita_rappresentazione_diversa_negata(self):
        # Multipiattaforma: "vista" simula un alias non visibile al
        # confronto dei path (\\?\, UNC loopback, bind mount) ma con la
        # stessa identita' filesystem di data.
        vista = self.app / "vista"
        vista.mkdir()
        identita_originale = filesystem_policy._identita
        identita_data = identita_originale(self.data.resolve())

        def identita_simulata(percorso):
            if Path(percorso) == vista.resolve():
                return identita_data
            return identita_originale(percorso)

        with mock.patch.object(filesystem_policy, "_identita", identita_simulata):
            self._assert_deny(self._decidi(vista))
            self._assert_deny(self._decidi(vista / "memory.json"))
            self._assert_deny(self._decidi("vista/nuovo.txt"))
            self._assert_allow(self._decidi(self.app / "prompt.txt"))

        # Senza alias simulato "vista" e' una directory qualunque.
        self._assert_allow(self._decidi(vista / "x.txt"), vista / "x.txt")

    def _prefissato(self) -> str:
        return "\\\\?\\" + str(self.app)

    @unittest.skipUnless(sys.platform == "win32", "prefisso \\\\?\\ solo su Windows")
    def test_allowed_root_con_prefisso_lungo(self):
        allowed = [self._prefissato()]

        for target in ("data", "data/memory.json", "data/nuovo.txt",
                       self._prefissato() + "\\data\\memory.json"):
            with self.subTest(target=target):
                self._assert_deny(self._decidi(target, allowed=allowed))
        self._assert_allow(self._decidi("prompt.txt", allowed=allowed))

    @unittest.skipUnless(sys.platform == "win32", "UNC loopback solo su Windows")
    def test_allowed_root_unc_loopback(self):
        app = str(self.app)
        unc = "\\\\localhost\\" + app[0] + "$" + app[2:]
        try:
            disponibile = Path(unc).is_dir()
        except OSError:
            disponibile = False
        if not disponibile:
            self.skipTest("share amministrativa UNC non disponibile in questo ambiente")

        allowed = [unc]
        for target in ("data/memory.json", unc + "\\data\\memory.json", "data"):
            with self.subTest(target=target):
                self._assert_deny(self._decidi(target, allowed=allowed))
        self._assert_allow(self._decidi("prompt.txt", allowed=allowed))


if __name__ == "__main__":
    unittest.main()
