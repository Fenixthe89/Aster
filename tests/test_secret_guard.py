"""
Secret Guard pre-0.7.2d - forme compatte di OTP, PIN e CVV.

Il guard analizza il content scritto dal modello, che spesso riscrive la
richiesta in forma compatta ("Ricordati il codice OTP 582913" diventa
"OTP 582913"). Per i soli valori numerici (OTP, PIN, CVV) una
parola-chiave seguita direttamente dal valore deve bastare; un numero
senza parola-chiave non deve mai essere bloccato.

Limiti noti, volutamente NON asseriti come ALLOW perché non sono un
comportamento desiderato:
- recovery code senza copula o al plurale ("recovery code 1234-5678"),
  rimandato a un micro-step dedicato;
- copula inglese "is" ("My OTP is 582913");
- parole tra parola-chiave e valore senza copula ("OTP della banca 582913");
- falsi positivi preesistenti della finestra con copula e della password
  ("La password è scaduta").

Nessun segreto reale: token e intestazioni PEM sono costruiti a runtime
con placeholder, le carte sono numeri di test pubblici. I test di
pipeline usano esclusivamente una directory temporanea isolata.

Esecuzione:
    python -m unittest tests.test_secret_guard -v
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from modules.memory import StatoMemoria, MODALITA_NORMALE, carica_archivio
from modules.memory_session import MemorySessionState
from modules.memory_tools import esegui_tool_memoria, rileva_contenuto_sensibile

LIMITE_RICERCA = 5


class GuardTestCase(unittest.TestCase):
    """Helper comuni per asserzioni su più input."""

    def assertCategoria(self, testi, categoria):
        for testo in testi:
            with self.subTest(testo=testo):
                self.assertEqual(rileva_contenuto_sensibile(testo), categoria)

    def assertConsentiti(self, testi):
        for testo in testi:
            with self.subTest(testo=testo):
                self.assertIsNone(rileva_contenuto_sensibile(testo))


# =====================================================================
# OTP / CODICI DI VERIFICA
# =====================================================================

class TestOtp(GuardTestCase):

    def test_forme_compatte(self):
        self.assertCategoria(
            [
                "OTP 582913",
                "codice OTP 582913",
                "Ricordati il codice OTP 582913",
                "codice monouso 582913",
                "password monouso 582913",
                "codice di verifica 582913",
                "codice verifica 582913",
                "verification code 582913",
                "one-time password 582913",
                "one time password 582913",
                "codice a due fattori 582913",
                "codice 2FA 582913",
            ],
            "otp",
        )

    def test_separatore_singolo(self):
        self.assertCategoria(
            [
                "OTP - 582913",
                "OTP – 582913",
                "OTP — 582913",
                "OTP #582913",
                "OTP-582913",
                'OTP "582913"',
                "OTP '582913'",
            ],
            "otp",
        )

    def test_copula(self):
        self.assertCategoria(
            [
                "il mio OTP è 582913",
                "OTP: 582913",
                "otp = 582913",
                "otp=582913",
                "codice verifica: 582913",
                "Il codice di verifica e' 582913",
                "L'OTP della banca è 582913",
            ],
            "otp",
        )

    def test_case_insensitive(self):
        self.assertCategoria(
            [
                "otp 582913",
                "Otp 582913",
                "CODICE DI VERIFICA 582913",
                "Codice Monouso 582913",
                "One-Time Password 582913",
                "VERIFICATION CODE 582913",
            ],
            "otp",
        )

    def test_whitespace(self):
        self.assertCategoria(
            [
                "OTP    582913",
                "OTP\t582913",
                "OTP 582913",
                "OTP \t 582913",
            ],
            "otp",
        )

    def test_newline(self):
        self.assertCategoria(
            [
                "OTP\n582913",
                "OTP\r\n582913",
                "OTP\n\n582913",
                "OTP:\n582913",
                "codice di verifica\n582913",
            ],
            "otp",
        )

    def test_raggruppato_tre_tre(self):
        self.assertCategoria(
            [
                "OTP 582 913",
                "OTP 582-913",
                "OTP: 582 913",
                "codice di verifica WhatsApp: 582-913",
            ],
            "otp",
        )

    def test_limiti_lunghezza(self):
        self.assertCategoria(["OTP 1234", "OTP 12345678"], "otp")
        self.assertConsentiti(["OTP 123", "OTP 123456789"])

    def test_keyword_senza_valore_adiacente(self):
        self.assertConsentiti(
            [
                "Cos'è un OTP?",
                "Uso Google Authenticator per gli OTP",
                "L'OTP scade dopo 3600 secondi",
                "Il codice OTP arriva sul numero 3331234567",
                "Il report OTP ha 2 pagine e 1500 parole",
                "Il codice di verifica ha 6 cifre",
                "Ho attivato la 2FA su GitHub nel 2024",
                "Il mio 2FA è attivo",
                "OTP Bank ha sede a Budapest dal 1949",
            ]
        )

    def test_one_time_password_con_copula_resta_password(self):
        # La regola password precede quella OTP e l'ordine non cambia:
        # il contenuto resta bloccato, con categoria password.
        self.assertEqual(
            rileva_contenuto_sensibile("one time password: 582913"),
            "password",
        )


# =====================================================================
# PIN
# =====================================================================

class TestPin(GuardTestCase):

    def test_forme_compatte(self):
        self.assertCategoria(
            [
                "PIN 1234",
                "codice PIN 4321",
                "PIN - 1234",
                "PIN #1234",
                "pin 1234",
                "Pin 1234",
            ],
            "pin",
        )

    def test_copula(self):
        self.assertCategoria(
            [
                "il PIN è 1234",
                "PIN: 1234",
                "Il PIN della carta e' 1234",
                "pin del telefono = 987654",
            ],
            "pin",
        )

    def test_newline_e_whitespace(self):
        self.assertCategoria(
            ["PIN\n1234", "PIN:\n1234", "PIN\t1234", "PIN    1234"],
            "pin",
        )

    def test_limiti_lunghezza(self):
        self.assertCategoria(["PIN 1234", "PIN 123456"], "pin")
        self.assertConsentiti(["PIN 123", "PIN 1234567"])

    def test_keyword_senza_valore_adiacente(self):
        self.assertConsentiti(
            [
                "Il mio PIN è cambiato",
                "Il PIN del modulo SIM è stato cambiato nel 2023",
                "Il PIN si blocca dopo 1000 tentativi",
                "Il pin 13 di Arduino",
            ]
        )


# =====================================================================
# CVV
# =====================================================================

class TestCvv(GuardTestCase):

    def test_forme_compatte(self):
        self.assertCategoria(
            ["CVV 123", "codice CVV 123", "CVV - 123", "cvv 123"],
            "cvv",
        )

    def test_copula(self):
        self.assertCategoria(
            ["CVV: 123", "il CVV della Visa è 456", "cvv=7890"],
            "cvv",
        )

    def test_newline_e_whitespace(self):
        self.assertCategoria(
            ["CVV\n123", "CVV:\n123", "CVV\t123", "CVV    123"],
            "cvv",
        )

    def test_limiti_lunghezza(self):
        self.assertCategoria(["CVV 123", "CVV 1234"], "cvv")
        self.assertConsentiti(["CVV 12", "CVV 12345"])

    def test_keyword_senza_valore_adiacente(self):
        self.assertConsentiti(
            [
                "Il CVV è sul retro della carta",
                "Cos'è il CVV?",
                "Il CVV cambia ogni 365 giorni",
            ]
        )


# =====================================================================
# NUMERI ORDINARI FUORI CONTESTO SENSIBILE
# =====================================================================

class TestNumeriOrdinari(GuardTestCase):

    def test_numeri_senza_parola_chiave(self):
        self.assertConsentiti(
            [
                "Il mio CAP è 35100",
                "Il ticket è 582913",
                "Il codice prodotto è 582913",
                "Il numero ordine è 582913",
                "Il mio ID cliente è 582913",
                "La build è 582913",
                "La porta è 8080",
                "Sono nato nel 1989",
                "La versione è 123456",
                "porta 1234",
                "anno 1234",
                "numero civico 123",
                "codice prodotto 1234",
                "Ho percorso 582913 metri",
                "Ricordati che il numero preferito è 582913",
                "Ticket #582913",
                "Ordine n. 582913",
                "Il codice sconto è 582913",
                "Il volo AZ 1234 parte alle 8",
                "Il mio numero di telefono è 3331234567",
                "Il server ascolta sulla porta 11434",
                "Il numero di serie è 1234-5678-9012",
                "La GPU è una RTX 3060 Ti con 8 GB",
            ]
        )


# =====================================================================
# REGRESSIONE: CATEGORIE NON TOCCATE DA QUESTO STEP
# =====================================================================

class TestRegressioneAltreCategorie(GuardTestCase):

    def test_password(self):
        self.assertCategoria(
            [
                "password: hunter2",
                "la mia password è hunter2",
                "La password di Gmail e' Pippo123!",
                "pwd=Segreta99",
                "La password del wifi è CasaRossa2024",
            ],
            "password",
        )
        self.assertConsentiti(["password manager: uso Bitwarden"])

    def test_api_key_e_token(self):
        # Costruiti a runtime: nessun token letterale nel sorgente.
        self.assertCategoria(
            [
                "API key: " + "sk-" + "0" * 24,
                "token API: " + "abcd1234" * 3,
                "Il token è " + "abcdefghijklmnop" + "1234",
                "bearer token: " + "eyJhbGciOiJIUzI1NiJ9" + "abcdef",
                "chiave AWS " + "AKIA" + "0" * 16,
                "ghp_" + "TEST" * 5 + " per GitHub",
            ],
            "api_key_or_token",
        )
        self.assertConsentiti(
            [
                "API key rotation ogni 90 giorni",
                "cos'è un token API?",
                "Il token API scade tra 30 giorni",
            ]
        )

    def test_chiave_privata_pem(self):
        testi = [
            "-----BEGIN " + tipo + "-----\nplaceholder\n-----END " + tipo + "-----"
            for tipo in (
                "PRIVATE KEY",
                "RSA PRIVATE KEY",
                "OPENSSH PRIVATE KEY",
                "EC PRIVATE KEY",
            )
        ]
        self.assertCategoria(testi, "private_key")
        self.assertConsentiti(["La private key sta nel file id_rsa"])

    def test_carta_luhn_valida(self):
        self.assertCategoria(
            [
                "carta 4111 1111 1111 1111",
                "carta di credito 4111-1111-1111-1111",
                "credit card 5555555555554444",
                "carta di debito 3782 822463 10005",
                "La carta è 4012888888881881",
            ],
            "payment_card",
        )

    def test_carta_luhn_non_valida(self):
        self.assertConsentiti(
            ["carta 4111 1111 1111 1112", "La mia carta fedeltà Coop"]
        )

    def test_seed_e_recovery_gia_supportati(self):
        self.assertCategoria(
            [
                "seed phrase: abandon ability able about above absent absorb abstract",
                "recovery code: 1234-5678",
                "codice di recupero: ABCD-1234",
                "backup code = ab12cd",
            ],
            "recovery_secret",
        )

    def test_codici_2fa_di_backup_restano_otp(self):
        self.assertEqual(
            rileva_contenuto_sensibile("codici 2FA di backup: 1234-5678"),
            "otp",
        )


# =====================================================================
# PIPELINE MEMORIA SU DIRECTORY TEMPORANEA
# =====================================================================

class TestPipelineMemoria(unittest.TestCase):
    """
    Il blocco avviene prima di pending e scrittura: l'archivio temporaneo
    resta byte-identico e il valore non compare nel risultato del tool.
    """

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix="aster_test_secret_guard_"))
        self.percorso_memoria = self.base_dir / "memory.json"

        # Mai dentro data/ reale.
        self.assertFalse(
            self.percorso_memoria.resolve().is_relative_to(
                (BASE_DIR / "data").resolve()
            )
        )

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _seed(self, memories, next_id):
        self.percorso_memoria.write_text(
            json.dumps(
                {
                    "meta": {"schema_version": 2, "next_memory_id": next_id},
                    "memories": memories,
                },
                ensure_ascii=False,
                indent=4,
            ),
            encoding="utf-8",
        )

    def _hash(self):
        return hashlib.sha256(self.percorso_memoria.read_bytes()).hexdigest()

    def _tool(self, nome_tool, argomenti, stato_sessione):
        return esegui_tool_memoria(
            nome_tool=nome_tool,
            argomenti=argomenti,
            stato_memoria=StatoMemoria(modalita=MODALITA_NORMALE, memoria=None),
            stato_sessione=stato_sessione,
            percorso_memoria=self.percorso_memoria,
            limite_ricerca=LIMITE_RICERCA,
        )

    def _assert_bloccato(self, risultato, categoria, valore, stato_sessione, hash_prima):
        self.assertEqual(risultato["status"], "blocked_sensitive")
        self.assertEqual(risultato["sensitive_category"], categoria)
        self.assertIsNone(stato_sessione.pending_action)
        self.assertEqual(self._hash(), hash_prima)
        self.assertNotIn(valore, json.dumps(risultato, ensure_ascii=False))

    def test_crea_explicit_otp(self):
        self._seed([], next_id=1)
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {"content": "OTP 582913", "mode": "explicit"},
            ss,
        )

        self._assert_bloccato(r, "otp", "582913", ss, hash_prima)
        self.assertEqual(carica_archivio(self.percorso_memoria)["memories"], [])

    def test_crea_proposal_otp(self):
        self._seed([], next_id=1)
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {"content": "OTP 582913", "mode": "proposal"},
            ss,
        )

        self._assert_bloccato(r, "otp", "582913", ss, hash_prima)
        self.assertEqual(carica_archivio(self.percorso_memoria)["memories"], [])

    def test_crea_explicit_cvv(self):
        self._seed([], next_id=1)
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {"content": "CVV 123", "mode": "explicit"},
            ss,
        )

        self._assert_bloccato(r, "cvv", "123", ss, hash_prima)

    def test_modifica_pin(self):
        self._seed(
            [
                {
                    "id": 1,
                    "content": "Contenuto originale",
                    "created_at": "2026-01-01T10:00:00+01:00",
                    "updated_at": "2026-01-01T10:00:00+01:00",
                }
            ],
            next_id=2,
        )
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "modifica_memoria",
            {"memory_id": 1, "new_content": "PIN 1234"},
            ss,
        )

        self._assert_bloccato(r, "pin", "1234", ss, hash_prima)
        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"][0]["content"], "Contenuto originale")

    def test_numero_ordinario_viene_salvato(self):
        self._seed([], next_id=1)
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {"content": "Il ticket è 582913", "mode": "explicit"},
            ss,
        )

        self.assertEqual(r["status"], "created")
        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"][0]["content"], "Il ticket è 582913")


if __name__ == "__main__":
    unittest.main()
