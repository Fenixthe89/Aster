"""Funzioni di interfaccia testuale di Aster."""


def stampa_banner(nome: str, versione: str, modello: str) -> None:
    """Mostra le informazioni iniziali di Aster."""

    print("=" * 55)
    print(f"{nome} CYBER AGENT v{versione}")
    print(f"Modello: {modello}")
    print("Bentornato, Sem.")
    print("Scrivi 'esci' per terminare.")
    print("=" * 55)