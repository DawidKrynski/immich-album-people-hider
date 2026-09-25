"""Hide Immich people who appear only in selected albums.

Faces in the excluded albums are still detected and clustered by Immich, so a
person who also appears anywhere else in the library stays (or becomes)
visible. Only people whose every counted photo sits in the excluded albums are
hidden. The tool remembers which people it hid, so it can unhide them later
and never touches people hidden or unhidden by hand.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Archived photos are part of the private library; locked-folder photos and
# hidden motion-photo parts are not counted as "elsewhere".
DEFAULT_VISIBILITIES = ("timeline", "archive")
PEOPLE_PAGE_SIZE = 500
UPDATE_BATCH_SIZE = 500
STATE_VERSION = 1


class ImmichError(RuntimeError):
    pass


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    is_hidden: bool
    is_favorite: bool

    @property
    def label(self) -> str:
        return self.name or "(unnamed)"


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    asset_count: int


class Api(Protocol):
    def list_albums(self) -> list[Album]: ...
    def list_people(self) -> list[Person]: ...
    def count_assets(self, person_id: str, album_ids: list[str], inside: bool) -> int: ...
    def set_hidden(self, changes: dict[str, bool]) -> list[str]: ...


class ImmichClient:
    """Minimal Immich API client (tested against Immich v3.2)."""

    def __init__(self, base_url: str, api_key: str, visibilities: Iterable[str] = DEFAULT_VISIBILITIES,
                 timeout: float = 60.0) -> None:
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/api"):
            base_url += "/api"
        self.base_url = base_url
        self.api_key = api_key
        self.visibilities = list(visibilities)
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: object | None = None,
                 query: dict[str, object] | None = None) -> object:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"x-api-key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")[:500]
            raise ImmichError(f"{method} {path} returned HTTP {error.code}: {details}") from error
        except urllib.error.URLError as error:
            raise ImmichError(f"Cannot reach Immich at {self.base_url}: {error.reason}") from error
        return json.loads(data) if data else None

    def list_albums(self) -> list[Album]:
        rows = self._request("GET", "/albums")
        return [Album(row["id"], row["albumName"], int(row.get("assetCount") or 0)) for row in rows]

    def list_people(self) -> list[Person]:
        people: list[Person] = []
        page = 1
        while True:
            data = self._request("GET", "/people", query={"withHidden": "true", "page": page, "size": PEOPLE_PAGE_SIZE})
            for row in data["people"]:
                people.append(Person(row["id"], row.get("name") or "", bool(row.get("isHidden")),
                                     bool(row.get("isFavorite"))))
            if not data.get("hasNextPage"):
                return people
            page += 1

    def count_assets(self, person_id: str, album_ids: list[str], inside: bool) -> int:
        """Count the person's assets inside (any of) or outside (none of) the albums."""
        search_filter = {
            "personIds": {"any": [person_id]},
            "albumIds": {"any" if inside else "none": album_ids},
            "visibility": {"in": self.visibilities},
        }
        data = self._request("POST", "/search/statistics", {"filter": search_filter})
        return int(data["total"])

    def set_hidden(self, changes: dict[str, bool]) -> list[str]:
        """Apply isHidden changes in one request; return IDs that failed."""
        items = [{"id": person_id, "isHidden": hidden} for person_id, hidden in changes.items()]
        results = self._request("PUT", "/people", {"people": items})
        return [row["id"] for row in results if not row.get("success")]


def resolve_albums(albums: list[Album], specs: list[str]) -> list[Album]:
    """Resolve album names (exact match) or IDs to albums."""
    resolved: dict[str, Album] = {}
    for spec in specs:
        matches = []
        if UUID_RE.match(spec):
            matches = [album for album in albums if album.id.lower() == spec.lower()]
        if not matches:
            matches = [album for album in albums if album.name == spec]
        if not matches:
            raise ImmichError(f"Album not found: {spec!r}")
        if len(matches) > 1:
            ids = ", ".join(album.id for album in matches)
            raise ImmichError(f"Album name {spec!r} is ambiguous ({ids}); pass the album ID instead")
        resolved[matches[0].id] = matches[0]
    return list(resolved.values())


# --- state -----------------------------------------------------------------

@dataclass
class State:
    # person id -> metadata about when the tool hid it
    hidden_by_tool: dict[str, dict] = field(default_factory=dict)
    # people the user unhid after the tool hid them; never hide them again
    kept_visible: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != STATE_VERSION:
            raise ImmichError(f"Unsupported state file version in {path}")
        return cls(data.get("hiddenByTool", {}), data.get("keptVisible", {}))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STATE_VERSION, "hiddenByTool": self.hidden_by_tool, "keptVisible": self.kept_visible}
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "immich-album-people-hider" / "state.json"


# --- planning ----------------------------------------------------------------

@dataclass
class Options:
    max_outside: int = 0
    include_named: bool = False
    include_favorites: bool = False


@dataclass
class Decision:
    person: Person
    outside: int
    inside: int | None = None


@dataclass
class Plan:
    hide: list[Decision] = field(default_factory=list)
    unhide: list[Decision] = field(default_factory=list)
    # tool-hidden people the user unhid by hand
    released: list[Person] = field(default_factory=list)
    # tool-hidden people that no longer exist (merged or deleted)
    vanished: list[str] = field(default_factory=list)
    # entries left over from an interrupted run whose change never took effect
    dropped: list[str] = field(default_factory=list)
    skipped_named: int = 0
    skipped_favorite: int = 0
    skipped_kept: int = 0


def build_plan(api: Api, people: list[Person], album_ids: list[str], state: State, options: Options) -> Plan:
    plan = Plan()
    by_id = {person.id: person for person in people}

    for person_id, meta in state.hidden_by_tool.items():
        person = by_id.get(person_id)
        if person is None:
            plan.vanished.append(person_id)
        elif not person.is_hidden:
            # An interrupted run leaves "pending"; only a clean entry means the user unhid it.
            if meta.get("pending"):
                plan.dropped.append(person_id)
            else:
                plan.released.append(person)
        else:
            meta.pop("pending", None)

    dropped = set(plan.dropped)
    for person in people:
        if person.id in state.hidden_by_tool and person.id not in dropped:
            if not person.is_hidden:
                continue
            outside = api.count_assets(person.id, album_ids, inside=False)
            if outside > options.max_outside:
                plan.unhide.append(Decision(person, outside))
            continue
        if person.is_hidden:
            continue  # hidden by hand; not ours
        if person.id in state.kept_visible:
            plan.skipped_kept += 1
            continue
        if person.name and not options.include_named:
            plan.skipped_named += 1
            continue
        if person.is_favorite and not options.include_favorites:
            plan.skipped_favorite += 1
            continue
        outside = api.count_assets(person.id, album_ids, inside=False)
        if outside > options.max_outside:
            continue
        # Only hide people who really appear in the excluded albums (not, for
        # example, people whose photos are all in the locked folder).
        inside = api.count_assets(person.id, album_ids, inside=True)
        if inside > 0:
            plan.hide.append(Decision(person, outside, inside))
    return plan


def _batches(items: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(items), UPDATE_BATCH_SIZE):
        yield items[start:start + UPDATE_BATCH_SIZE]


def apply_plan(api: Api, plan: Plan, state: State, album_ids: list[str]) -> list[str]:
    """Apply a plan, updating state in place after every batch.

    Each change is first recorded as pending, so a run interrupted mid-way
    (network error, crash) never leaves people hidden that the state does not
    know about, and an unknown outcome is resolved on the next run.
    Returns IDs whose update Immich reported as failed.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    for person_id in plan.vanished + plan.dropped:
        state.hidden_by_tool.pop(person_id, None)
    for person in plan.released:
        state.hidden_by_tool.pop(person.id, None)
        state.kept_visible[person.id] = {"since": now}

    failed: list[str] = []
    for batch in _batches([d.person.id for d in plan.hide]):
        for pid in batch:
            state.hidden_by_tool[pid] = {"hiddenAt": now, "albums": album_ids, "pending": "hide"}
        batch_failed = set(api.set_hidden({pid: True for pid in batch}))
        for pid in batch:
            if pid in batch_failed:
                state.hidden_by_tool.pop(pid, None)
            else:
                state.hidden_by_tool[pid].pop("pending", None)
        failed.extend(batch_failed)
    for batch in _batches([d.person.id for d in plan.unhide]):
        for pid in batch:
            state.hidden_by_tool[pid]["pending"] = "unhide"
        batch_failed = set(api.set_hidden({pid: False for pid in batch}))
        for pid in batch:
            if pid in batch_failed:
                state.hidden_by_tool[pid].pop("pending", None)
            else:
                state.hidden_by_tool.pop(pid, None)
        failed.extend(batch_failed)
    return sorted(failed)


def restore_all(api: Api, people: list[Person], state: State) -> tuple[list[str], list[str]]:
    """Unhide everyone this tool hid. Returns (unhidden, failed)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    by_id = {person.id: person for person in people}
    targets: list[str] = []
    for pid, meta in list(state.hidden_by_tool.items()):
        person = by_id.get(pid)
        if person is None:
            state.hidden_by_tool.pop(pid)
        elif not person.is_hidden:
            state.hidden_by_tool.pop(pid)
            if not meta.get("pending"):
                state.kept_visible[pid] = {"since": now}
        else:
            targets.append(pid)
    unhidden: list[str] = []
    failed: list[str] = []
    for batch in _batches(targets):
        for pid in batch:
            state.hidden_by_tool[pid]["pending"] = "unhide"
        batch_failed = set(api.set_hidden({pid: False for pid in batch}))
        for pid in batch:
            if pid in batch_failed:
                state.hidden_by_tool[pid].pop("pending", None)
                failed.append(pid)
            else:
                state.hidden_by_tool.pop(pid)
                unhidden.append(pid)
    return unhidden, sorted(failed)
