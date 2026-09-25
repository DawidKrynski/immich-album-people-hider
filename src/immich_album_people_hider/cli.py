from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .core import (
    DEFAULT_VISIBILITIES,
    ImmichClient,
    ImmichError,
    Options,
    State,
    apply_plan,
    build_plan,
    default_state_path,
    resolve_albums,
    restore_all,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="immich-album-people-hider",
        description="Hide Immich people who appear only in selected albums (e.g. a big event or a meme album). "
                    "People who also appear anywhere else stay visible. Dry run unless --apply is given.",
    )
    parser.add_argument("--url", default=os.environ.get("IMMICH_URL"),
                        help="Immich server URL, e.g. http://127.0.0.1:2283 (env IMMICH_URL)")
    parser.add_argument("--api-key-file", type=Path,
                        help="file containing the API key (otherwise env IMMICH_API_KEY)")
    parser.add_argument("--album", action="append", default=[], metavar="NAME_OR_ID",
                        help="excluded album, exact name or ID; repeatable (env IMMICH_EXCLUDED_ALBUMS, "
                             "one per line)")
    parser.add_argument("--apply", action="store_true", help="actually change people (default: dry run)")
    parser.add_argument("--max-outside", type=int, default=0, metavar="N",
                        help="hide people with at most N photos outside the excluded albums (default 0)")
    parser.add_argument("--include-named", action="store_true", help="also hide people who have a name")
    parser.add_argument("--include-favorites", action="store_true", help="also hide favorite people")
    parser.add_argument("--count-visibility", action="append", choices=["timeline", "archive", "hidden", "locked"],
                        help="asset visibilities counted as 'elsewhere' (default: timeline and archive)")
    parser.add_argument("--state-file", type=Path, default=None,
                        help=f"where to remember which people this tool hid (default {default_state_path()})")
    parser.add_argument("--restore", action="store_true",
                        help="unhide every person this tool has hidden and exit (honours --apply)")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every affected person")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if not args.album and os.environ.get("IMMICH_EXCLUDED_ALBUMS"):
        args.album = [line.strip() for line in os.environ["IMMICH_EXCLUDED_ALBUMS"].splitlines() if line.strip()]
    if not args.url:
        parser.error("--url or IMMICH_URL is required")
    if not args.album and not args.restore:
        parser.error("at least one --album is required")
    if args.max_outside < 0:
        parser.error("--max-outside must be >= 0")
    return args


def read_api_key(args: argparse.Namespace) -> str:
    if args.api_key_file:
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    else:
        key = os.environ.get("IMMICH_API_KEY", "").strip()
    if not key:
        raise ImmichError("No API key: set IMMICH_API_KEY or pass --api-key-file")
    return key


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = args.state_file or default_state_path()
    mode = "APPLY" if args.apply else "DRY RUN"
    try:
        client = ImmichClient(args.url, read_api_key(args), args.count_visibility or DEFAULT_VISIBILITIES)
        state = State.load(state_path)
        people = client.list_people()

        if args.restore:
            if not args.apply:
                targets = [p for p in people if p.id in state.hidden_by_tool and p.is_hidden]
                print(f"[{mode}] would unhide {len(targets)} people hidden by this tool")
                return 0
            try:
                unhidden, failed = restore_all(client, people, state)
            finally:
                state.save(state_path)
            print(f"[{mode}] unhid {len(unhidden)} people; {len(failed)} failed")
            return 2 if failed else 0

        albums = resolve_albums(client.list_albums(), args.album)
        album_ids = [album.id for album in albums]
        print(f"[{mode}] excluded albums: " + ", ".join(f"{a.name} ({a.asset_count} assets)" for a in albums))
        print(f"[{mode}] {len(people)} people, {sum(p.is_hidden for p in people)} already hidden")

        options = Options(args.max_outside, args.include_named, args.include_favorites)
        plan = build_plan(client, people, album_ids, state, options)

        print(f"[{mode}] hide: {len(plan.hide)}, unhide: {len(plan.unhide)}, "
              f"released by user: {len(plan.released)}, vanished: {len(plan.vanished)}, interrupted: {len(plan.dropped)}, "
              f"skipped named: {plan.skipped_named}, favorite: {plan.skipped_favorite}, "
              f"kept visible by user: {plan.skipped_kept}")
        if args.verbose:
            for d in plan.hide:
                print(f"  hide   {d.person.id} {d.person.label}: {d.inside} in albums, {d.outside} elsewhere")
            for d in plan.unhide:
                print(f"  unhide {d.person.id} {d.person.label}: {d.outside} elsewhere")
            for p in plan.released:
                print(f"  user unhid {p.id} {p.label}; will not hide again")

        if not args.apply:
            print(f"[{mode}] nothing changed; re-run with --apply")
            return 0
        try:
            failed = apply_plan(client, plan, state, album_ids)
        finally:
            state.save(state_path)  # keep progress even if a later batch fails
        print(f"[{mode}] done; state saved to {state_path}; {len(failed)} updates failed")
        return 2 if failed else 0
    except ImmichError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
