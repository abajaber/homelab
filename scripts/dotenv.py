"""Tiny dotenv parser + ${VAR} substitution for the TrueNAS reconciler.

Used to inject per-app secrets from `servers/<host>/apps/<app>/.env` into the
compose body before it's sent to TrueNAS. We avoid python-dotenv to keep the
dependency footprint identical to the rest of the script tree.

Substitution uses string.Template.safe_substitute, which handles `$VAR` and
`${VAR}`. Compose's default-value (`${VAR:-default}`) and required-with-error
(`${VAR:?msg}`) syntaxes are NOT supported — none of the apps in this repo
need them. If that changes, swap to a real interpolator.
"""

from __future__ import annotations

import re
import string


_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_VAR_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def parse(text: str) -> dict[str, str]:
    """Parse a KEY=VALUE dotenv body. Comments (`#...`) and blank lines ignored.

    Quoted values (single or double) have their wrapping quotes stripped. No
    escape-sequence interpretation, no `\n`-expansion — what's between the
    quotes is taken verbatim. That's enough for the secret values we hold.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _DOTENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # Strip an inline `# comment` only if the value isn't quoted.
        if not (val.startswith(('"', "'")) and val.endswith(val[0]) and len(val) >= 2):
            hash_idx = val.find(" #")
            if hash_idx != -1:
                val = val[:hash_idx].rstrip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        out[key] = val
    return out


def substitute(compose_text: str, env: dict[str, str]) -> tuple[str, list[str]]:
    """Return (rendered, unresolved). `unresolved` lists vars referenced in
    `compose_text` that aren't in `env`, deduped, in first-seen order.
    """
    rendered = string.Template(compose_text).safe_substitute(env)
    unresolved: list[str] = []
    seen: set[str] = set()
    for m in _VAR_REF.finditer(rendered):
        name = m.group(1) or m.group(2)
        if name and name not in env and name not in seen:
            seen.add(name)
            unresolved.append(name)
    return rendered, unresolved


if __name__ == "__main__":
    sample_env = """\
# example
FOO=bar
BAZ="hello world"
QUUX='single quoted'
EMPTY=
WITH_EQUALS=a=b=c
"""
    sample_compose = """\
services:
  x:
    environment:
      A: ${FOO}
      B: $BAZ
      C: ${MISSING}
      D: ${QUUX}
      E: ${WITH_EQUALS}
"""
    env = parse(sample_env)
    assert env == {
        "FOO": "bar",
        "BAZ": "hello world",
        "QUUX": "single quoted",
        "EMPTY": "",
        "WITH_EQUALS": "a=b=c",
    }, env
    rendered, unresolved = substitute(sample_compose, env)
    assert "A: bar" in rendered
    assert "B: hello world" in rendered
    assert "${MISSING}" in rendered
    assert "D: single quoted" in rendered
    assert "E: a=b=c" in rendered
    assert unresolved == ["MISSING"], unresolved
    print("dotenv.py self-test ok")
