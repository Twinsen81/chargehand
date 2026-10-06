"""The only module that knows the banksman command line.

banksman is the lease pool: an external tool that leases devices, emulators, and build
slots to the runs on one machine. chargehand runs its command and reads the JSON that it
prints, and never imports it. An import would add a runtime dependency, and a copy of
banksman inside chargehand's own environment can differ from the command that the
repository's scripts call, while both work on the same lease files.

Every JSON document that banksman prints carries a ``schema`` number, which banksman
changes on any incompatible change. Output with a number that is not in :data:`SCHEMAS`
is refused rather than read on a guess.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from chargehand.errors import PoolError
from chargehand.pool.base import Lease, Pool

SCHEMAS = frozenset({5})


class BanksmanPool(Pool):
    """A thin, testable wrapper. The fake `banksman` in the test suite replaces the command."""

    def __init__(self, binary: str = "banksman", *, timeout_secs: float = 120.0) -> None:
        self.binary = binary
        self.timeout_secs = timeout_secs

    def _json(self, command: str) -> Mapping[str, Any]:
        argv = (self.binary, command, "--json")
        shown = " ".join(argv)
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_secs,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PoolError(f"'{self.binary}' not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise PoolError(f"`{shown}` timed out after {exc.timeout:.0f}s") from exc
        except OSError as exc:
            raise PoolError(f"could not run `{shown}`: {exc}") from exc
        if completed.returncode != 0:
            raise PoolError(
                f"`{shown}` exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[:500]}"
            )
        try:
            data = json.loads(completed.stdout)
        except ValueError:
            raise PoolError(f"`{shown}` did not print JSON") from None
        if not isinstance(data, Mapping):
            raise PoolError(f"`{shown}` printed JSON that is not an object")
        schema = data.get("schema")
        if not _is_number(schema) or schema not in SCHEMAS:
            raise PoolError(_unsupported(shown, schema))
        return data

    def version(self) -> str:
        """For `doctor`: raises PoolError when this chargehand cannot read the output."""
        return _text(self._json("version"), "version", "banksman version")

    def reap(self) -> list[str]:
        where = "banksman reap"
        return [
            f"{_text(entry, 'resource', where)}: {_text(entry, 'outcome', where)}"
            for entry in _objects(self._json("reap"), "reaped", where)
        ]

    def status(self) -> list[Lease]:
        where = "banksman status"
        leases = []
        for entry in _objects(self._json("status"), "resources", where):
            lease = entry.get("lease")
            if lease is None:
                continue
            if not isinstance(lease, Mapping):
                raise PoolError(f"{where}: a lease is not an object")
            leases.append(
                Lease(
                    resource=_text(lease, "resource", where),
                    kind=_text(lease, "kind", where),
                    state=_text(lease, "state", where),
                    owner=_text(lease, "owner", where),
                    issue=_optional_text(lease, "issue", where),
                    expires_in_secs=_abandoned_in(lease),
                )
            )
        return leases


def _is_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unsupported(command: str, schema: object) -> str:
    supported = ", ".join(str(number) for number in sorted(SCHEMAS))
    if not isinstance(schema, int) or isinstance(schema, bool):
        return f"`{command}` printed no schema number; this chargehand reads schema {supported}"
    older = "chargehand" if schema > max(SCHEMAS) else "banksman"
    return (
        f"`{command}` printed schema {schema}, and this chargehand reads schema {supported}; "
        f"upgrade {older}"
    )


def _objects(data: Mapping[str, Any], key: str, where: str) -> Sequence[Mapping[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise PoolError(f"{where}: '{key}' is not a list of objects")
    return value


def _text(node: Mapping[str, Any], key: str, where: str) -> str:
    value = node.get(key)
    if not isinstance(value, str):
        raise PoolError(f"{where}: '{key}' is missing or not text")
    return value


def _optional_text(node: Mapping[str, Any], key: str, where: str) -> str | None:
    return None if node.get(key) is None else _text(node, key, where)


def _abandoned_in(lease: Mapping[str, Any]) -> float | None:
    """When the lease becomes void if nothing touches it again; a draining lease has none."""
    free_by = lease.get("free_by")
    abandoned = free_by.get("abandoned") if isinstance(free_by, Mapping) else None
    seconds = abandoned.get("in") if isinstance(abandoned, Mapping) else None
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return float(seconds)
    return None
