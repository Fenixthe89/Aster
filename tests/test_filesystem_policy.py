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

Esecuzione:
    python -m unittest discover -s tests -v
"""

import pathlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

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
        decisione = risolvi_path_autorizzato("file.txt", [])

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "no_allowed_roots")

    def test_tutte_root_invalide_deny(self):
        inesistente = self.base_dir / "non_esiste"
        decisione = risolvi_path_autorizzato(
            "file.txt", [str(inesistente), "relativo", ""]
        )

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "invalid_allowed_root")


# =====================================================================
# ROOT VALIDATION
# =====================================================================

class TestRootValidation(FilesystemPolicyTestCase):

    def test_root_assoluta_esistente_directory_valida(self):
        decisione = risolvi_path_autorizzato("file.txt", [self.base_dir])

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.reason, "ok")

    def test_root_inesistente_non_concede_accesso(self):
        inesistente = self.base_dir / "non_esiste_davvero"
        decisione = risolvi_path_autorizzato("file.txt", [inesistente])

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_relativa_non_concede_accesso(self):
        decisione = risolvi_path_autorizzato("file.txt", ["workspace_relativo"])

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_stringa_vuota_non_concede_accesso(self):
        decisione = risolvi_path_autorizzato("file.txt", [""])

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_punto_non_concede_accesso(self):
        decisione = risolvi_path_autorizzato("file.txt", ["."])

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_che_punta_a_file_non_concede_accesso(self):
        file_non_directory = self.base_dir / "sono_un_file.txt"
        file_non_directory.write_text("contenuto")

        decisione = risolvi_path_autorizzato("altro.txt", [file_non_directory])

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "invalid_allowed_root")

    def test_root_duplicate_dedup_corretto(self):
        # Due voci diverse che risolvono alla stessa root: deve
        # comportarsi come un'unica root valida (path relativo non
        # ambiguo).
        decisione = risolvi_path_autorizzato(
            "file.txt", [self.base_dir, str(self.base_dir)]
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
        decisione = risolvi_path_autorizzato(str(target), [self.root])

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.resolved_path, target.resolve())
        self.assertEqual(decisione.reason, "ok")

    def test_path_assoluto_fuori_root_deny(self):
        target = self.fuori / "segreto.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root])

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_sottocartella_dentro_root_allow(self):
        target = self.root / "sub" / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root])

        self.assertTrue(decisione.allowed)

    def test_traversal_che_resta_dentro_allow(self):
        (self.root / "sub").mkdir()
        target_str = str(self.root / "sub" / ".." / "altro.txt")

        decisione = risolvi_path_autorizzato(target_str, [self.root])

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.resolved_path, (self.root / "altro.txt").resolve())

    def test_traversal_che_esce_deny(self):
        target_str = str(self.root / ".." / "fuori" / "segreto.txt")

        decisione = risolvi_path_autorizzato(target_str, [self.root])

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_target_inesistente_dentro_root_allow(self):
        target = self.root / "non_esiste_ancora" / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root])

        self.assertTrue(decisione.allowed)
        self.assertEqual(decisione.reason, "ok")

    def test_target_inesistente_fuori_root_deny(self):
        target = self.fuori / "non_esiste" / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root])

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
        decisione = risolvi_path_autorizzato("sub/file.txt", [self.root_a])

        self.assertTrue(decisione.allowed)
        self.assertEqual(
            decisione.resolved_path, (self.root_a / "sub" / "file.txt").resolve()
        )

    def test_relativo_con_una_root_valida_deny_se_esce(self):
        decisione = risolvi_path_autorizzato("../fuori.txt", [self.root_a])

        self.assertFalse(decisione.allowed)
        self.assertEqual(decisione.reason, "outside_allowed_roots")

    def test_relativo_con_piu_root_valide_ambiguo(self):
        decisione = risolvi_path_autorizzato(
            "sub/file.txt", [self.root_a, self.root_b]
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
        decisione = risolvi_path_autorizzato(str(target), [self.root_a, self.root_b])

        self.assertTrue(decisione.allowed)

    def test_assoluto_nella_seconda_root_allow(self):
        target = self.root_b / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root_a, self.root_b])

        self.assertTrue(decisione.allowed)

    def test_assoluto_fuori_da_entrambe_deny(self):
        target = self.fuori / "file.txt"
        decisione = risolvi_path_autorizzato(str(target), [self.root_a, self.root_b])

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
            str(self.root / "link_esterno" / "segreto.txt"), [self.root]
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
            str(self.root / "link_interno" / "file.txt"), [self.root]
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
                decisione = risolvi_path_autorizzato(path_richiesto, allowed_roots)
                if not decisione.allowed:
                    self.assertIsNone(decisione.resolved_path)

    def test_reason_non_contiene_path_esterno_richiesto(self):
        target_esterno = str(self.esterno / "informazione_privata.txt")
        decisione = risolvi_path_autorizzato(target_esterno, [self.root])

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
        decisione = risolvi_path_autorizzato("", [self.base_dir])

        self.assertFalse(decisione.allowed)
        self.assertIsNone(decisione.resolved_path)
        self.assertEqual(decisione.reason, "invalid_path")

    def test_path_richiesto_tipo_invalido_deny(self):
        for valore in (None, 123, ["a", "b"], {"x": 1}):
            with self.subTest(valore=valore):
                decisione = risolvi_path_autorizzato(valore, [self.base_dir])
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
                f"{marcatore}/file.txt", [self.base_dir]
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


if __name__ == "__main__":
    unittest.main()
