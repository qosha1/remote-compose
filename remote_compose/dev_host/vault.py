"""StartSimpli vault lookup for the GitHub PAT `rc dev up` clones with.

Why the vault at all: the alternative is every operator keeping a long-lived
`gho_`/`ghp_` PAT in their shell and hoping it never lands anywhere it
shouldn't. The vault already holds one, and `simpli` is already installed and
authenticated on the machines that run `rc dev up`, so reading it here removes
the "paste your token into the command line" step entirely.

Where the exchange happens matters. `sentinal/deploy/prod/rc.yml` fixes the
house model for this: vault is source-of-truth, something with credentials
exchanges it, and the *runtime host* never talks to the vault --
"NO simpli CLI in the images, NO runtime vault dependency, NO SIMPLI_TOKEN".
A dev box is a runtime host, so the exchange belongs here, CLI-side, on the
laptop that already has `simpli` configured. Nothing about the vault reaches
EC2 -- only the one value it hands back.

Everything in this module is best-effort by contract. `simpli` missing, not
configured, offline, or holding no GitHub key are all ordinary outcomes, not
errors: `rc dev up` falls back to --gh-token / $GH_TOKEN and provisions fine.
No function here raises, and no return value or reason string ever carries a
secret.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Key names to try inside the vault environment, most-intentional first.
#
# No key is named for this purpose yet, and rc must not write to the vault to
# create one -- so the list ends at the GitHub PAT that does exist today,
# GITHUB_VERCEL_PAT, named for whatever it was first minted for. Measured
# against api.github.com: that token is fine-grained and cannot see the
# debugg-ai repos `rc dev up` normally clones, which is precisely why
# token_can_read() exists rather than a blind hand-off. Add RC_DEV_GH_TOKEN or
# GH_TOKEN to the vault env and it wins automatically, no code change.
GH_TOKEN_VAULT_KEYS: tuple[str, ...] = (
    "RC_DEV_GH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT",
    "GH_PAT",
    "GITHUB_VERCEL_PAT",
)

# `simpli exchange` is a network round-trip. Cap it so an unreachable vault
# costs a few seconds of `rc dev up`, not a hung provision.
EXCHANGE_TIMEOUT_S = 20


@dataclass(frozen=True)
class VaultLookup:
    """Outcome of a vault lookup: either a token, or a reason there isn't one.

    Truthy iff a token was found, so callers read as
    `if result: ... else: <fall back>`.
    """

    token: str = ""
    key: str = ""  # vault key the token came from
    env: str = ""  # vault environment slug it was exchanged from
    reason: str = ""  # why there is no token — safe to print

    def __bool__(self) -> bool:
        return bool(self.token)

    def __repr__(self) -> str:
        # rc-h40: a dataclass holding a live PAT will eventually be printed by
        # something (an exception, a debugger, a log line). Redact at the type.
        return (
            f"VaultLookup(token={'<redacted>' if self.token else ''!r}, "
            f"key={self.key!r}, env={self.env!r}, reason={self.reason!r})"
        )

    @property
    def origin(self) -> str:
        """Human-readable provenance for `rc dev up` to echo. No secret."""
        return f"StartSimpli vault ({self.env}:{self.key})" if self else ""


def simpli_config_path() -> Path:
    """Mirror the simpli CLI's own config resolution (@startsimpli/cli dist/index.js).

    $SIMPLI_CONFIG_DIR, else $XDG_CONFIG_HOME/simpli, else ~/.config/simpli.
    """
    base = os.environ.get("SIMPLI_CONFIG_DIR")
    if not base:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        base = str(Path(xdg) / "simpli")
    return Path(base) / "config.json"


def configured_vault_envs() -> list[str]:
    """Environment slugs configured locally for simpli.

    Reads only the KEYS of the `environments` map. The access credential lives
    at environments[slug].key in the same file and is deliberately never
    touched -- `simpli exchange` reads it for us, so rc has no reason to.
    """
    try:
        cfg = json.loads(simpli_config_path().read_text())
    except (OSError, ValueError):
        return []
    envs = cfg.get("environments")
    return sorted(envs) if isinstance(envs, dict) else []


def _resolve_env_slug(env_slug: str | None) -> tuple[str, str]:
    """Pick the vault environment to exchange. Returns (slug, reason-if-none)."""
    if env_slug:
        return env_slug, ""
    envs = configured_vault_envs()
    if len(envs) == 1:
        # The overwhelmingly common case: one machine, one dev vault env.
        return envs[0], ""
    if not envs:
        return "", (
            f"no simpli environment configured in {simpli_config_path()} "
            f"— run `simpli configure` or pass --vault-env"
        )
    return "", (
        f"{len(envs)} simpli environments configured ({', '.join(envs)}) "
        f"— pass --vault-env to choose one"
    )


def _exchange(env_slug: str, timeout: int) -> tuple[dict | None, str]:
    """Run `simpli exchange creds` and return (secrets-map, reason-if-none).

    Reads the exchange on stdout rather than via the CLI's `-o <file>` so the
    bundle never touches the operator's disk. The subprocess's stdout/stderr
    are NEVER folded into the returned reason -- stdout is the secret bundle
    itself, and a truncated stderr buys little over the exit code.
    """
    exe = shutil.which("simpli")
    if not exe:
        return None, "simpli CLI not on PATH"
    try:
        proc = subprocess.run(
            [exe, "exchange", "creds", "-e", env_slug, "-f", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"simpli exchange timed out after {timeout}s"
    except OSError as exc:
        return None, f"could not run simpli: {type(exc).__name__}"
    if proc.returncode != 0:
        return None, (
            f"simpli exchange exited {proc.returncode} for env {env_slug!r} "
            f"— check `simpli envs` / $SIMPLI_TOKEN"
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, "simpli exchange did not return JSON"
    if not isinstance(data, dict):
        return None, "simpli exchange returned a non-object payload"
    return data, ""


def resolve_gh_token(
    env_slug: str | None = None,
    key_names: tuple[str, ...] | None = None,
    timeout: int = EXCHANGE_TIMEOUT_S,
) -> VaultLookup:
    """Best-effort: fetch a GitHub PAT from the StartSimpli vault.

    Never raises. Every failure comes back as a VaultLookup carrying a `reason`
    the caller can print verbatim -- it contains no secret material.
    """
    keys = key_names or GH_TOKEN_VAULT_KEYS
    slug, reason = _resolve_env_slug(env_slug)
    if not slug:
        return VaultLookup(reason=reason)
    secrets, reason = _exchange(slug, timeout)
    if secrets is None:
        return VaultLookup(reason=reason)
    for key in keys:
        value = secrets.get(key)
        if isinstance(value, str) and value.strip():
            return VaultLookup(token=value.strip(), key=key, env=slug)
    return VaultLookup(
        reason=(
            f"vault env {slug!r} has no GitHub-token key "
            f"(looked for: {', '.join(keys)})"
        )
    )


def github_slug(url: str) -> str:
    """owner/name for a github.com clone URL, or "" if it isn't one."""
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix) :].strip("/")
            if slug.endswith(".git"):
                slug = slug[:-4]
            return slug if slug.count("/") == 1 else ""
    return ""


def token_can_read(
    token: str, repo_slugs: list[str], timeout: int = 8
) -> tuple[bool, str]:
    """Can this token see every repo we're about to ask a box to clone?

    Not vault-specific -- it grades any candidate token. Worth the round-trip
    because the alternative is discovering the answer six minutes later, on the
    box, as a clone that 403s inside cloud-init with nothing on the operator's
    terminal to explain it. A fine-grained PAT that reads one org and not
    another is the normal case, not a corner one.

    Inconclusive counts as yes. If this machine can't reach api.github.com that
    says nothing about the token — the box does the cloning, not us — so a
    local network problem must not veto a credential that would have worked.
    """
    import urllib.error
    import urllib.request

    for slug in repo_slugs:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{slug}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "rc-dev",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=timeout).close()
        except urllib.error.HTTPError as exc:
            # GitHub answers 404, not 403, for a repo a token cannot see — it
            # declines to confirm the repo exists at all.
            return False, f"cannot read {slug} (HTTP {exc.code})"
        except (urllib.error.URLError, OSError):
            return True, ""
    return True, ""
