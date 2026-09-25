# immich-album-people-hider

Hide [Immich](https://immich.app) people who appear **only** in selected albums, such as a big New Year's Eve party, a wedding, a conference or a meme dump, while keeping everyone else visible.

Immich keeps detecting and clustering faces in those albums. As a result, someone from the event who also appears in your own photos is still recognised as the same person, and all their photos stay together. The tool only hides the people you would never want suggested on the Explore/People pages: people whose every photo is inside the excluded albums.

Related Immich discussions: [#9089](https://github.com/immich-app/immich/discussions/9089) (exclude folders from face recognition), [#5928](https://github.com/immich-app/immich/discussions/5928) (mass-hide strangers), [#13868](https://github.com/immich-app/immich/discussions/13868).

## How it works

For every visible, unnamed, non-favorite person:

1. Count the person's photos **outside** the excluded albums, using a single `POST /api/search/statistics` call with `personIds.any` + `albumIds.none`.
2. If that count is `0` (or `<= --max-outside`) and the person appears in at least one excluded album, set `isHidden = true` (`PUT /api/people`).

The tool records the people it hid in a small state file, which lets it:

- **unhide** a person later if they appear in a photo outside the excluded albums (for example, you upload a new photo with them),
- **never touch** people you hid or unhid by hand. If you unhide someone the tool hid, it remembers that and won't hide them again,
- undo everything with `--restore` (stop any timer first, or the next run hides them again),
- recover safely from interrupted runs: every change is recorded as pending before it is sent.

Named and favorite people are never hidden unless you pass `--include-named` / `--include-favorites`. Archived photos count as "elsewhere". Locked-folder photos do not.

Nothing is deleted. The tool only flips the person's *hidden* flag, which you can also change in *People → Show & hide people*.

## Requirements

- Immich **v3.2 or newer**. The tool uses the v3.2 search filter API.
- Python 3.10+, no dependencies.
- An API key with these permissions: `person.read`, `person.update`, `album.read`, `asset.statistics`.
  Create it in *Account Settings → API Keys*. People are per user, so use a key of the user whose people you want to clean up.

## Usage

```bash
pip install git+https://github.com/DawidKrynski/immich-album-people-hider
# or download immich-album-people-hider.pyz from the releases page and run it with python3

export IMMICH_URL=http://127.0.0.1:2283
export IMMICH_API_KEY=...            # or --api-key-file /path/to/key

# dry run (default): shows what would change
immich-album-people-hider --album "New Year's Eve Party 2022" -v

# apply
immich-album-people-hider --album "New Year's Eve Party 2022" --album "Memes" --apply

# undo everything this tool did
immich-album-people-hider --restore --apply
```

| Option | Meaning |
|---|---|
| `--album NAME_OR_ID` | Excluded album, by exact name or ID. Repeatable. |
| `--apply` | Change people. Without it the run is a dry run. |
| `--max-outside N` | Also hide people with at most `N` photos elsewhere (default `0`). |
| `--include-named`, `--include-favorites` | Allow hiding named and favorite people. |
| `--count-visibility` | Which asset visibilities count as "elsewhere" (default `timeline`, `archive`). |
| `--state-file PATH` | Default `~/.local/state/immich-album-people-hider/state.json`. |
| `--restore` | Unhide everyone the tool has hidden. |

Exit codes: `0` success, `1` error, `2` some updates failed.

### Running it regularly

New uploads create new people, so run the tool after face recognition, e.g. daily. [`deploy/`](deploy/) has a hardened systemd service and timer (`DynamicUser`, API key via `LoadCredential`). Build the single-file executable with `scripts/build-pyz.sh`.

## Limitations

- A photo that is in an excluded album **and** in another album still counts as inside the excluded album.
- If a stranger from the event is wrongly merged with someone from your own photos, that person stays visible. Fix the merge in Immich; the next run takes care of the rest.
- A full facial-recognition re-run creates new person IDs. The next run of the tool simply hides the new ones.
- Immich does not list people with fewer faces than *Minimum recognized faces*, so the tool doesn't see or need to hide them.

## Development

```bash
python -m pytest     # tests use a fake API and a local HTTP stub, no Immich needed
```

Tested against a real Immich 3.2.1 instance with ~750 people.

## License

MIT
