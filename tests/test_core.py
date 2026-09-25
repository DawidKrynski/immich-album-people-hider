import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from immich_album_people_hider.cli import main
from immich_album_people_hider.core import (
    Album,
    ImmichClient,
    ImmichError,
    Options,
    Person,
    State,
    apply_plan,
    build_plan,
    resolve_albums,
    restore_all,
)

EVENT = "11111111-1111-4111-8111-111111111111"
MEMES = "22222222-2222-4222-8222-222222222222"


class FakeApi:
    """person id -> list of album-id sets, one per asset (empty set = in no album)."""

    def __init__(self, people, assets):
        self.people = {p.id: p for p in people}
        self.assets = assets
        self.updates = []
        self.fail = set()

    def list_people(self):
        return list(self.people.values())

    def count_assets(self, person_id, album_ids, inside):
        rows = self.assets.get(person_id, [])
        if inside:
            return sum(1 for albums in rows if albums & set(album_ids))
        return sum(1 for albums in rows if not albums & set(album_ids))

    def set_hidden(self, changes):
        self.updates.append(dict(changes))
        for pid, hidden in changes.items():
            if pid not in self.fail:
                p = self.people[pid]
                self.people[pid] = Person(p.id, p.name, hidden, p.is_favorite)
        return [pid for pid in changes if pid in self.fail]


def person(pid, name="", hidden=False, favorite=False):
    return Person(pid, name, hidden, favorite)


def run(api, state, options=None, albums=(EVENT,)):
    plan = build_plan(api, api.list_people(), list(albums), state, options or Options())
    failed = apply_plan(api, plan, state, list(albums))
    return plan, failed


def test_hides_only_people_exclusive_to_album():
    api = FakeApi(
        [person("a"), person("b"), person("c")],
        {"a": [{EVENT}, {EVENT}], "b": [{EVENT}, set()], "c": [set()]},
    )
    state = State()
    plan, failed = run(api, state)
    assert [d.person.id for d in plan.hide] == ["a"]
    assert failed == []
    assert api.people["a"].is_hidden and not api.people["b"].is_hidden
    assert set(state.hidden_by_tool) == {"a"}


def test_photo_in_other_album_counts_as_elsewhere_but_both_excluded_albums_do_not():
    api = FakeApi([person("a"), person("b")], {"a": [{EVENT, MEMES}], "b": [{EVENT}, {"other"}]})
    plan, _ = run(api, State(), albums=(EVENT, MEMES))
    assert [d.person.id for d in plan.hide] == ["a"]


def test_skips_named_favorite_and_manually_hidden_people():
    api = FakeApi(
        [person("n", name="Ala"), person("f", favorite=True), person("h", hidden=True)],
        {pid: [{EVENT}] for pid in "nfh"},
    )
    plan, _ = run(api, State())
    assert plan.hide == [] and plan.skipped_named == 1 and plan.skipped_favorite == 1
    plan, _ = run(api, State(), Options(include_named=True, include_favorites=True))
    assert sorted(d.person.id for d in plan.hide) == ["f", "n"]


def test_people_not_in_album_at_all_are_left_alone():
    api = FakeApi([person("locked-only")], {"locked-only": []})
    plan, _ = run(api, State())
    assert plan.hide == []


def test_unhides_when_person_appears_elsewhere_later():
    api = FakeApi([person("a")], {"a": [{EVENT}]})
    state = State()
    run(api, state)
    api.assets["a"].append(set())
    plan, _ = run(api, state)
    assert [d.person.id for d in plan.unhide] == ["a"]
    assert not api.people["a"].is_hidden and state.hidden_by_tool == {}


def test_manual_unhide_is_respected_forever():
    api = FakeApi([person("a")], {"a": [{EVENT}]})
    state = State()
    run(api, state)
    api.people["a"] = person("a", hidden=False)  # user unhid it in the UI
    plan, _ = run(api, state)
    assert [p.id for p in plan.released] == ["a"] and plan.hide == []
    plan, _ = run(api, state)
    assert plan.hide == [] and plan.skipped_kept == 1


def test_max_outside_threshold():
    api = FakeApi([person("a")], {"a": [{EVENT}, set()]})
    plan, _ = run(api, State(), Options(max_outside=1))
    assert [d.person.id for d in plan.hide] == ["a"]


def test_vanished_person_is_dropped_from_state():
    api = FakeApi([], {})
    state = State(hidden_by_tool={"gone": {}})
    plan, _ = run(api, state)
    assert plan.vanished == ["gone"] and state.hidden_by_tool == {}


def test_failed_update_is_not_recorded():
    api = FakeApi([person("a"), person("b")], {"a": [{EVENT}], "b": [{EVENT}]})
    api.fail = {"b"}
    state = State()
    _, failed = run(api, state)
    assert failed == ["b"] and set(state.hidden_by_tool) == {"a"}


def test_restore_unhides_only_tool_hidden_people():
    api = FakeApi([person("a"), person("m", hidden=True)], {"a": [{EVENT}], "m": [{EVENT}]})
    state = State()
    run(api, state)
    unhidden, failed = restore_all(api, api.list_people(), state)
    assert unhidden == ["a"] and failed == []
    assert api.people["m"].is_hidden and state.hidden_by_tool == {}


def test_state_roundtrip(tmp_path):
    path = tmp_path / "sub" / "state.json"
    State(hidden_by_tool={"a": {"hiddenAt": "x"}}, kept_visible={"b": {}}).save(path)
    loaded = State.load(path)
    assert loaded.hidden_by_tool == {"a": {"hiddenAt": "x"}} and loaded.kept_visible == {"b": {}}


def test_resolve_albums():
    albums = [Album(EVENT, "New Year's Eve Party", 900), Album(MEMES, "Memes", 50), Album("x", "Dup", 1), Album("y", "Dup", 1)]
    assert [a.id for a in resolve_albums(albums, ["New Year's Eve Party", MEMES, EVENT])] == [EVENT, MEMES]
    with pytest.raises(ImmichError, match="not found"):
        resolve_albums(albums, ["Nope"])
    with pytest.raises(ImmichError, match="ambiguous"):
        resolve_albums(albums, ["Dup"])


# --- HTTP level: checks the exact requests sent to Immich --------------------

class Server:
    def __init__(self):
        self.requests = []
        self.people = [
            {"id": "p1", "name": "", "isHidden": False, "isFavorite": False},
            {"id": "p2", "name": "", "isHidden": False, "isFavorite": False},
        ]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else None

            def do_GET(self):
                outer.requests.append(("GET", self.path, None, self.headers.get("x-api-key")))
                if self.path == "/api/albums":
                    self._send([{"id": EVENT, "albumName": "New Year's Eve Party", "assetCount": 3}])
                elif self.path.startswith("/api/people?"):
                    self._send({"people": outer.people, "total": 2, "hidden": 0, "hasNextPage": False})
                else:
                    self._send({"message": "nope"}, 404)

            def do_POST(self):
                body = self._body()
                outer.requests.append(("POST", self.path, body, None))
                f = body["filter"]
                pid = f["personIds"]["any"][0]
                inside = "any" in f["albumIds"]
                # p1: only in the event album; p2: also elsewhere
                total = {("p1", True): 3, ("p1", False): 0, ("p2", True): 1, ("p2", False): 5}[(pid, inside)]
                self._send({"total": total})

            def do_PUT(self):
                body = self._body()
                outer.requests.append(("PUT", self.path, body, None))
                for item in body["people"]:
                    for p in outer.people:
                        if p["id"] == item["id"]:
                            p["isHidden"] = item["isHidden"]
                self._send([{"id": item["id"], "success": True} for item in body["people"]])

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.httpd.shutdown()


def test_client_request_shapes(server):
    client = ImmichClient(server.url, "secret")
    assert client.count_assets("p1", [EVENT], inside=False) == 0
    method, path, body, _ = server.requests[-1]
    assert (method, path) == ("POST", "/api/search/statistics")
    assert body == {"filter": {"personIds": {"any": ["p1"]}, "albumIds": {"none": [EVENT]},
                               "visibility": {"in": ["timeline", "archive"]}}}


def test_cli_dry_run_then_apply(server, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IMMICH_API_KEY", "secret")
    state = tmp_path / "state.json"
    args = ["--url", server.url, "--album", "New Year's Eve Party", "--state-file", str(state), "-v"]
    assert main(args) == 0
    assert not any(r[0] == "PUT" for r in server.requests) and not state.exists()
    assert "hide   p1" in capsys.readouterr().out
    assert main(args + ["--apply"]) == 0
    puts = [r for r in server.requests if r[0] == "PUT"]
    assert puts[-1][2] == {"people": [{"id": "p1", "isHidden": True}]}
    assert set(json.loads(state.read_text())["hiddenByTool"]) == {"p1"}
    assert all(r[3] == "secret" for r in server.requests if r[0] == "GET")
    assert main(["--url", server.url, "--restore", "--apply", "--state-file", str(state)]) == 0
    assert server.people[0]["isHidden"] is False


class FlakyApi(FakeApi):
    """Raises on the n-th set_hidden call, after optionally applying it."""

    def __init__(self, *args, fail_call, applied=False):
        super().__init__(*args)
        self.fail_call, self.applied, self.calls = fail_call, applied, 0

    def set_hidden(self, changes):
        self.calls += 1
        if self.calls == self.fail_call:
            if self.applied:
                super().set_hidden(changes)
            raise ImmichError("boom")
        return super().set_hidden(changes)


def test_interrupted_run_keeps_completed_batches(monkeypatch):
    monkeypatch.setattr("immich_album_people_hider.core.UPDATE_BATCH_SIZE", 1)
    api = FlakyApi([person("a"), person("b")], {"a": [{EVENT}], "b": [{EVENT}]}, fail_call=2)
    state = State()
    with pytest.raises(ImmichError):
        run(api, state)
    assert state.hidden_by_tool["a"].get("pending") is None
    assert state.hidden_by_tool["b"]["pending"] == "hide"
    # next run: b was never hidden, so it is dropped (not treated as a manual unhide) and retried
    api.fail_call = 0
    plan, _ = run(api, state)
    assert plan.dropped == ["b"] and plan.released == [] and [d.person.id for d in plan.hide] == ["b"]
    assert set(state.hidden_by_tool) == {"a", "b"} and state.kept_visible == {}


def test_unknown_outcome_of_hide_is_confirmed_next_run():
    api = FlakyApi([person("a")], {"a": [{EVENT}]}, fail_call=1, applied=True)
    state = State()
    with pytest.raises(ImmichError):
        run(api, state)
    plan, _ = run(api, state)
    assert plan.hide == [] and "pending" not in state.hidden_by_tool["a"]


def test_restore_remembers_manual_unhide():
    api = FakeApi([person("a")], {"a": [{EVENT}]})
    state = State()
    run(api, state)
    api.people["a"] = person("a", hidden=False)
    unhidden, _ = restore_all(api, api.list_people(), state)
    assert unhidden == [] and "a" in state.kept_visible


def test_album_named_like_uuid():
    albums = [Album("real-id", EVENT, 1)]
    assert [a.id for a in resolve_albums(albums, [EVENT])] == ["real-id"]
