"""
Secret Guard pre-0.7.2d - forme compatte di OTP, PIN, CVV e recovery code.

Il guard analizza il content scritto dal modello, che spesso riscrive la
richiesta in forma compatta ("Ricordati il codice OTP 582913" diventa
"OTP 582913", "Ricordati il recovery code 1234-5678" diventa
"recovery code 1234-5678"). Per i soli valori strutturati (OTP, PIN, CVV,
recovery/backup code e recovery key) una parola-chiave seguita
direttamente dal valore deve bastare; un valore senza parola-chiave non
deve mai essere bloccato.

Limiti noti, volutamente NON asseriti come ALLOW perché non sono un
comportamento desiderato:
- copula inglese "is" ("My OTP is 582913");
- parole tra parola-chiave e valore senza copula ("OTP della banca 582913");
- recovery code di sole lettere ("ABCD-EFGH"), alfanumerici contigui
  ("AB12CD34"), gruppi con " - " o con en dash, liste markdown
  ("- 1234-5678"), copula plurale "sono";
- "backup key" non è una parola-chiave recovery;
- falsi positivi preesistenti della finestra con copula, della password e
  del controllo recovery storico ("La password è scaduta",
  "Il recovery code è lungo 8 caratteri");
- falsi positivi residui accettati del ramo recovery adiacente
  ("backup codes 2024-2025", "codice backup 20240315").

Nessun segreto reale: token e intestazioni PEM sono costruiti a runtime
con placeholder, le carte sono numeri di test pubblici. I test di
pipeline usano esclusivamente una directory temporanea isolata.

Esecuzione:
    python -m unittest tests.test_secret_guard -v
"""

import hashlib
import json
import re
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
from modules.memory_tools import (
    _RECOVERY_CODE_PATTERN,
    esegui_tool_memoria,
    rileva_contenuto_sensibile,
)

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
# RECOVERY / BACKUP CODE E RECOVERY KEY
# =====================================================================

# Chiavi sintetiche nei formati BitLocker (8 gruppi da 6 cifre) e Apple
# (7 gruppi da 4 caratteri): nessuna chiave reale.
BITLOCKER_FINTA = "-".join(
    ["123456", "234567", "345678", "456789", "567890", "678901", "789012", "890123"]
)
APPLE_FINTA = "-".join(["XK4P", "9Q2M", "7R3T", "W8Y1", "5N6B", "2C4D", "8F9G"])


class TestRecoveryCode(GuardTestCase):

    def test_recovery_code(self):
        self.assertCategoria(
            [
                "recovery code 1234-5678",
                "recovery codes 1234-5678",
                "recovery code: AB12-CD34",
                "recovery code 12345678",
                "recovery code = 12345678",
                "recovery code a1b2c-3d4e5",
                "recovery code #AB12-CD34-EF56",
                "2FA recovery code 1234-5678",
                "RECOVERY CODE 1234-5678",
                "Recovery Codes: 1234-5678",
            ],
            "recovery_secret",
        )

    def test_backup_code(self):
        self.assertCategoria(
            [
                "backup code 1234-5678",
                "backup codes 1234-5678",
                "backup codes: 1234-5678",
                "backup codes - 12345678",
                "backup code — 9876-5432",
                'backup codes "1234-5678"',
                "2FA backup code 1234-5678",
                "BACKUP CODE AB12CD34-EF56",
                "Backup codes di GitHub: a1b2c-3d4e5 f6g7h-8i9j0",
                "i miei backup codes Google sono: 1234 5678",
            ],
            "recovery_secret",
        )

    def test_italiano(self):
        self.assertCategoria(
            [
                "codice di recupero 1234-5678",
                "codici di recupero 1234 5678",
                "codici di recupero: 1234 5678, 8765 4321",
                "codice backup AB12-CD34",
                "codici backup 1234-5678",
                "codice di backup 12345678",
                "codici di backup 1234-5678 8765-4321",
                "codice backup 2FA AB12-CD34",
                "CODICE DI RECUPERO 1234-5678",
                "Codice Di Recupero 1234-5678",
            ],
            "recovery_secret",
        )

    def test_recovery_key(self):
        self.assertCategoria(
            [
                "recovery key " + BITLOCKER_FINTA,
                "La recovery key di BitLocker è " + BITLOCKER_FINTA,
                "recovery key " + APPLE_FINTA,
                "recovery keys: " + APPLE_FINTA,
                "chiave di recupero " + APPLE_FINTA,
                "chiavi di recupero: " + APPLE_FINTA,
                "chiave di ripristino " + BITLOCKER_FINTA,
                "chiave di ripristino BitLocker: " + BITLOCKER_FINTA,
                "chiavi di ripristino " + BITLOCKER_FINTA,
            ],
            "recovery_secret",
        )

    def test_formati_valore(self):
        self.assertCategoria(
            [
                # gruppi uniti da trattino, almeno una cifra nel token
                "recovery code 1234-5678",
                "recovery code AB12-CD34",
                "recovery code ABCD-1234",
                "recovery code ABCD1-EFGH2-IJKL3-MNOP4-QRST5",
                "recovery code 1234abcd-EFGH",
                "recovery code 1234-5678-9012-3456-7890-1234-5678-9012",
                # gruppi separati da uno spazio, una cifra in ogni gruppo
                "recovery code 1234 5678",
                "recovery code A1B2 C3D4",
                # cifre contigue
                "recovery code 12345678",
                "recovery code 123456789012",
            ],
            "recovery_secret",
        )

    def test_multiline_e_whitespace(self):
        self.assertCategoria(
            [
                "backup codes:\n1234-5678\n8765-4321",
                "recovery code\n1234-5678",
                "codici di recupero:\n\n1234 5678\n8765 4321",
                "Codice di backup GitHub:\r\na1b2c-3d4e5",
                "recovery codes =\nAB12-CD34\nEF56-GH78",
                "recovery code    1234-5678",
                "recovery code\t1234 5678",
                "recovery code 1234-5678",
            ],
            "recovery_secret",
        )

    def test_codici_multipli(self):
        # Basta un codice riconosciuto per bloccare l'intero contenuto.
        self.assertCategoria(
            [
                "backup codes: 1234-5678 8765-4321 1111-2222",
                "recovery codes 1234 5678 8765 4321",
                "codici di recupero: 1234-5678, 8765-4321, 1111-2222",
                "backup codes: a1b2c-3d4e5; f6g7h-8i9j0",
                "codici backup\n1234-5678\n8765-4321\n1111-2222",
            ],
            "recovery_secret",
        )

    def test_valori_senza_parola_chiave(self):
        self.assertConsentiti(
            [
                "Il ticket è 1234-5678",
                "Ordine 1234-5678",
                "Codice prodotto AB12-CD34",
                "codice prodotto AB12-CD34",
                "Versione ABCD-EFGH",
                "Build 1234-5678",
                "ID pratica 1234-5678",
                "Il codice sconto è AB12-CD34",
                "Il numero di serie è 1234-5678-9012",
                "Telefono 1234 5678",
                "backup giornaliero 1234-5678",
                "Il backup del NAS gira alle 0300",
                "Il recovery mode del BIOS usa il tasto F12",
            ]
        )

    def test_parola_chiave_senza_valore(self):
        self.assertConsentiti(
            [
                "Cos'è un recovery code?",
                "Come funzionano i backup codes?",
                "I recovery code sono importanti",
                "Ho 8 recovery code",
                "Ho 8 recovery code su GitHub",
                "recovery code example",
                "backup code generation",
                "recovery code format",
                "recovery code format guide",
                "password manager backup",
                "backup key rotation",
                "database recovery key",
                "La chiave di backup del database è un concetto importante",
                "codice fiscale e codice di recupero sono cose diverse",
                "codice di backup: in cassaforte",
                "I codici di backup vanno stampati e tenuti al sicuro dal 2024",
                "I backup codes sono 10",
                "recovery codes 10",
                "codici di recupero: 8 da 4 cifre",
            ]
        )

    def test_gap_con_parole(self):
        self.assertConsentiti(
            [
                "recovery code valido per 1234-5678 utenti",
                "backup codes 2024 rigenerati",
                "backup codes edizione 2024-2025",
                "Il codice di recupero arriva via SMS al 3331234567",
            ]
        )

    def test_valori_troppo_corti(self):
        self.assertConsentiti(
            [
                "backup code 1234",
                "recovery code 1234567",
                "recovery code AB1",
                "recovery code ABCD",
                "recovery code 123-456",
            ]
        )

    def test_valori_troppo_lunghi(self):
        self.assertConsentiti(
            [
                "recovery code 1234567890123",
                "recovery code 123456789-1234",
                "recovery code 1234-123456789",
                "recovery code 1234-5678-9012-3456-7890-1234-5678-9012-3456",
            ]
        )

    def test_separatori_non_supportati(self):
        self.assertConsentiti(
            [
                "recovery code 1234/5678",
                "recovery code 1234.5678",
                "recovery code 1234_5678",
            ]
        )

    def test_token_tecnici(self):
        self.assertConsentiti(
            [
                "backup code RFC-6238",
                "backup code ISO-8601",
                "backup code UTF-8",
                "recovery codes x86-64",
                "recovery code v2-2024",
                "backup code open-source",
                "backup code self-hosted",
                "recovery code Windows 11",
                "recovery code Windows11",
                "backup code Office365",
                "recovery code format 2024",
            ]
        )

    def test_seed_phrase_invariata(self):
        self.assertCategoria(
            [
                "recovery phrase: abandon ability able about above absent absorb abstract",
                "mnemonic abandon ability able about above absent absorb abstract",
            ],
            "recovery_secret",
        )

    def test_ordine_dei_controlli_invariato(self):
        # Il check recovery è l'ultimo: le categorie precedenti prevalgono.
        self.assertEqual(
            rileva_contenuto_sensibile("codice di recupero 2FA 1234-5678"),
            "otp",
        )
        self.assertEqual(
            rileva_contenuto_sensibile(
                "carta 4111 1111 1111 1111 e recovery code 1234-5678"
            ),
            "payment_card",
        )
        self.assertEqual(
            rileva_contenuto_sensibile("PIN 1234 e recovery code 1234-5678"),
            "pin",
        )


class TestRecoveryCodeConfineValore(GuardTestCase):
    """
    Il valore recovery va consumato per intero: il nuovo check non deve
    bloccare un prefisso valido di un token più lungo non supportato. Il
    pattern viene verificato anche direttamente, per distinguerlo dal
    controllo storico con copula.
    """

    def assertNuovoCheck(self, testi, corrisponde):
        for testo in testi:
            with self.subTest(testo=testo):
                trovato = _RECOVERY_CODE_PATTERN.search(testo) is not None
                self.assertEqual(trovato, corrisponde)

    def test_prefissi_di_token_piu_lunghi(self):
        testi = [
            # nono e decimo gruppo nella forma con spazio
            "recovery code 1234 5678 9012 3456 7890 1234 5678 9012 3456",
            "recovery code\n1234 5678 9012 3456 7890 1234 5678 9012 3456",
            "recovery code 1234 5678 9012 3456 7890 1234 5678 9012 3456 7890",
            # ultimo gruppo sovralungo nella forma con spazio
            "recovery code 1234 5678 123456789",
            "recovery code 1234 5678 9012abcdefgh",
            "recovery code 12345678 123456789",
            # continuazioni da token attaccate al valore
            "recovery code 1234-5678/9012",
            "recovery code 1234-5678.9012",
            "recovery code 1234-5678_9012",
            "recovery code 1234-5678@example",
            "recovery code 1234-5678\\9012",
            "recovery code 1234-5678+9012",
            "recovery code 1234-5678–9012",
            "recovery code 1234-5678.pdf",
            "recovery code 12345678/9012",
            "recovery code 12345678.9012",
            # token alfanumerico immediatamente più lungo
            "recovery code 1234-5678-ABCDEFGHIJ",
            "recovery code 1234-56789abcd",
            "recovery code 1234-5678é",
        ]
        self.assertNuovoCheck(testi, False)
        # Senza copula (è, e', :, =) il controllo storico non interviene.
        self.assertConsentiti(testi)

    def test_prefisso_con_copula_escluso_dal_nuovo_check(self):
        # Con una copula il controllo storico, invariato, può bloccare
        # questi casi per conto proprio: qui si verifica solo il nuovo
        # pattern.
        self.assertNuovoCheck(
            [
                "recovery code 1234-5678:9012",
                "recovery code: 1234-5678/9012",
                "recovery code: 1234 5678 9012 3456 7890 1234 5678 9012 3456",
            ],
            False,
        )

    def test_forma_con_spazio_da_due_a_otto_gruppi(self):
        testi = [
            "recovery code 1234 5678",
            "recovery code 1234 5678 9012 3456 7890 1234 5678 9012",
        ]
        self.assertNuovoCheck(testi, True)
        self.assertCategoria(testi, "recovery_secret")

    def test_valore_seguito_da_punteggiatura_o_testo(self):
        testi = [
            "recovery code 1234-5678, salvato ieri",
            "recovery code 1234-5678.",
            "recovery code 1234-5678. Salvato ieri",
            "(recovery code 1234-5678)",
            "recovery code 1234-5678;",
            "recovery code 1234-5678\nL'ho salvato ieri",
            "recovery code 1234 5678.",
            "recovery code 1234 5678\nL'ho salvato ieri",
            "recovery code 1234 5678 salvato nel cassetto",
            "recovery code 12345678, poi basta",
        ]
        self.assertNuovoCheck(testi, True)
        self.assertCategoria(testi, "recovery_secret")

    def test_lista_con_trattino_resta_bloccata(self):
        testi = [
            "recovery code 1234-5678 8765-4321",
            "backup codes: a1b2c-3d4e5 f6g7h-8i9j0",
        ]
        self.assertNuovoCheck(testi, True)
        self.assertCategoria(testi, "recovery_secret")


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

        # Né il segreto completo né alcuno dei suoi gruppi compaiono nel
        # risultato, che contiene solo testi fissi privi di cifre.
        testo = json.dumps(risultato, ensure_ascii=False)
        frammenti = [valore] + [g for g in re.split(r"[\s\-]+", valore) if g]
        for frammento in frammenti:
            with self.subTest(frammento=frammento):
                self.assertNotIn(frammento, testo)

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

    def test_crea_explicit_recovery_code(self):
        self._seed([], next_id=1)
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {"content": "recovery code 1234-5678", "mode": "explicit"},
            ss,
        )

        self._assert_bloccato(r, "recovery_secret", "1234-5678", ss, hash_prima)
        self.assertEqual(carica_archivio(self.percorso_memoria)["memories"], [])

    def test_crea_proposal_backup_codes(self):
        self._seed([], next_id=1)
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {"content": "backup codes: 1234-5678 8765-4321", "mode": "proposal"},
            ss,
        )

        self._assert_bloccato(
            r, "recovery_secret", "1234-5678 8765-4321", ss, hash_prima
        )
        self.assertEqual(carica_archivio(self.percorso_memoria)["memories"], [])

    def test_modifica_codice_di_recupero(self):
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
            {"memory_id": 1, "new_content": "codice di recupero 1234 5678"},
            ss,
        )

        self._assert_bloccato(r, "recovery_secret", "1234 5678", ss, hash_prima)
        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"][0]["content"], "Contenuto originale")

    def test_crea_explicit_recovery_key_bitlocker(self):
        self._seed([], next_id=1)
        hash_prima = self._hash()
        ss = MemorySessionState()

        r = self._tool(
            "crea_memoria",
            {
                "content": "La recovery key di BitLocker è " + BITLOCKER_FINTA,
                "mode": "explicit",
            },
            ss,
        )

        self._assert_bloccato(r, "recovery_secret", BITLOCKER_FINTA, ss, hash_prima)
        self.assertEqual(carica_archivio(self.percorso_memoria)["memories"], [])


if __name__ == "__main__":
    unittest.main()
