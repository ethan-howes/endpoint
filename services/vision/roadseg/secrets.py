"""Load local API keys from a gitignored KEY=value file into os.environ.

The services in this repo authenticate with vendor SDKs that read the key from
the environment themselves -- google-genai's `genai.Client()` looks for
GEMINI_API_KEY or GOOGLE_API_KEY. Rather than have every script thread a key
through its own argparse, this module copies a secrets file into os.environ
once, and the SDK finds it with no further wiring.

Resolution order for any name, highest priority first:
    1. the real environment, if the variable is already set and non-empty
    2. the secrets file
    3. nothing -- ValueError

Public API:
    DEFAULT_PATH     the service-root secrets.env, resolved from __file__
    parse_secrets()  KEY=value text -> dict
    load_secrets()   populate os.environ, return what was set
    get_secret()     read one name, env first
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH: Path = Path(__file__).resolve().parent.parent / "secrets.env"
"""Service-root secrets file. __file__-relative on purpose.

Resolving from __file__ rather than the process cwd means the file is found no
matter where a script is launched from. The cwd-relative default in
scripts/run_segmentation.py is the counterexample: launching it from elsewhere
scatters a stray results/ tree.
"""

_QUOTES = ("'", '"')


def parse_secrets(text: str) -> dict[str, str]:
    """Parse dotenv-style KEY=value text into a dict.

    Deliberately small: it handles what a hand-edited secrets file actually
    contains and nothing more. Blank lines and lines whose first non-space
    character is # are skipped. Each remaining line splits on its first =, so a
    value may itself contain =. One layer of matching quotes is stripped, and a
    leading `export ` is dropped.

    Inline `#` comments are NOT stripped: a value like `abc#def` is legal and
    there is no reliable way to tell it from a trailing comment.

    Args:
        text: full file contents.

    Returns:
        Mapping of name to value, in file order. Later assignments to the same
        name win. Names that are empty after stripping are skipped.
    """
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        if name:
            found[name] = value
    return found


def _read(path: Path | str | None) -> dict[str, str]:
    """Read and parse a secrets file.

    Args:
        path: file to read, or None for DEFAULT_PATH.

    Returns:
        The parsed mapping, or an empty one when the file does not exist.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        return parse_secrets(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def load_secrets(
    path: Path | str | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    """Copy secrets-file entries into os.environ.

    Intended to be called once at the top of a script so the vendor SDK picks
    the key up on its own.

    Args:
        path: secrets file to read, or None for DEFAULT_PATH.
        override: when False (the default) an already-set environment variable
            wins, so a per-shell export always beats the file. When True the
            file wins. Either way an existing but *empty* variable is treated
            as unset and gets filled in, which is what makes the placeholder
            `GEMINI_API_KEY=` in a freshly created file work.

    Returns:
        The name -> value mapping that is now visible in os.environ. Empty when
        the file is absent; a missing file is not an error here, since the key
        may legitimately come from the environment instead.
    """
    values = _read(path)
    applied: dict[str, str] = {}
    for name, value in values.items():
        if not value:
            continue
        if not override and os.environ.get(name):
            continue
        os.environ[name] = value
        applied[name] = value
    return applied


def get_secret(name: str, path: Path | str | None = None) -> str:
    """Read one secret, preferring the real environment over the file.

    Args:
        name: variable name, e.g. "GEMINI_API_KEY".
        path: secrets file to read, or None for DEFAULT_PATH.

    Returns:
        The non-empty value.

    Raises:
        ValueError: when the name resolves to nothing, or only to an empty
            value. The message names the variable and the file that was
            searched so the fix is obvious.
    """
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env

    target = Path(path) if path is not None else DEFAULT_PATH
    value = _read(target).get(name, "").strip()
    if value:
        return value

    raise ValueError(
        f"{name} is not set. Put it in {target} as {name}=<value>, or export "
        f"{name} in your shell."
    )
