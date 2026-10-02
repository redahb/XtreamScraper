"""The core artwork manager.

Scraper plugins supply normalized candidates, the metadata merge layer selects a winner
per slot (:mod:`..metadata.artwork_selection`), and this module decides what happens to
it: a local file, a remote NFO reference, or nothing (disabled). It also owns the
bookkeeping that makes later changes safe.

Two rules hold everywhere:

* A working artwork representation is never destroyed before its replacement exists:
  local files are written and verified before managed NFO references are removed, and
  NFO references are written and verified before managed local files are deleted.
* Only artwork this application can prove it owns (a database record with the same path
  and file hash, or the same NFO location and URL) is ever replaced or deleted. Unknown
  files and references are treated as user artwork and kept.

Work is done per item in two phases so a caller can fold the NFO changes into its own
single NFO write: :meth:`ArtworkManager.prepare` downloads/writes local files and plans
NFO edits; :meth:`ArtworkManager.complete` writes the remaining NFO edits, verifies them
and only then performs the deferred clean-up (deletions, ownership updates).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from ..config.settings import Settings
from ..filesystem import atomic
from ..metadata.artwork_selection import Slot
from ..metadata.models import Artwork, ArtworkType
from ..metadata.plugin import ArtworkPayload
from ..nfo.artwork_refs import RefLocation, add_ref, find_urls, remove_ref
from ..nfo.document import NfoDocument, NfoError
from ..nfo.paths import NfoKind
from ..nfo.service import NfoOwner, NfoSeed, update_nfo
from ..storage.artwork import (
    DISABLED,
    ERROR,
    EXTERNAL,
    KEPT,
    MANAGED,
    MODIFIED,
    MODIFIED_EXTERNALLY,
    OK,
    ArtworkRepository,
    FileRecord,
    ItemKey,
    SlotRecord,
)
from ..storage.db import Database
from ..storage.nfo_files import NfoFileRepository
from ..utils.redact import redact
from .download import ArtworkDownloader, ArtworkDownloadError, detect_format, validate_image
from .naming import (
    EPISODE,
    FORMAT_EXTENSIONS,
    MOVIE,
    SEASON,
    SERIES,
    TYPES_BY_KIND,
    ArtworkTarget,
    LocalName,
    all_local_names,
    enabled_types,
    existing_variants,
    local_names,
    nfo_locations,
)

log = logging.getLogger(__name__)
MAX_MESSAGES = 500
LOCAL, REMOTE = "local", "remote"
PrivateFetch = Callable[[str, str], Optional[ArtworkPayload]]


def file_hash(path: str) -> Optional[str]:
    try:
        return hashlib.sha256(atomic.read_bytes(path)).hexdigest()
    except OSError:
        return None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _base(path: str) -> str:
    return os.path.splitext(path)[0]


@dataclass
class ArtworkStats:
    considered: int = 0
    downloaded: int = 0
    nfo_urls_written: int = 0
    local_removed: int = 0
    nfo_refs_removed: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_MESSAGES:
            self.warnings.append(redact(message))

    def error(self, message: str) -> None:
        self.errors_count += 1
        if len(self.errors) < MAX_MESSAGES:
            self.errors.append(redact(message))

    def counters(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}

    def summary(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in ("considered", "downloaded", "nfo_urls_written", "local_removed",
                                              "nfo_refs_removed", "unchanged", "skipped")}


@dataclass
class NfoEdit:
    """Pending reference changes for one NFO file."""

    nfo_path: str
    nfo_kind: NfoKind
    seed_title: Optional[str] = None
    adds: list[tuple[RefLocation, str, int, Optional[str]]] = field(default_factory=list)  # loc, url, slot, plugin
    removes: list[tuple[RefLocation, str, int]] = field(default_factory=list)  # loc, url, ref id
    owner_key: Optional[ItemKey] = None  # the item an NFO this edit creates belongs to

    @property
    def changes(self) -> bool:
        return bool(self.adds or self.removes)

    def apply(self, doc: NfoDocument) -> None:
        for loc, url, _ in self.removes:
            remove_ref(doc, loc, url)
        for loc, url, _, _ in self.adds:
            add_ref(doc, loc, url)

    def verify(self, doc: NfoDocument) -> bool:
        added = all(url in find_urls(doc, loc) for loc, url, _, _ in self.adds)
        removed = all(url not in find_urls(doc, loc) for loc, url, _ in self.removes
                      if not any(a[0] == loc and a[1] == url for a in self.adds))
        return added and removed


@dataclass
class PostAction:
    """Clean-up that may only run once the listed NFO files were written successfully."""

    requires: tuple[str, ...]
    run: Callable[[], None]
    blocked: Callable[[], None]


@dataclass
class ItemPlan:
    target: ArtworkTarget
    edits: dict[str, NfoEdit] = field(default_factory=dict)
    post: list[PostAction] = field(default_factory=list)
    #: artwork type -> (source plugin, public URL, download_ref) of this run's selections that
    #: offer an authenticated download. Kept in memory only; never persisted.
    private: dict[str, tuple[str, str, str]] = field(default_factory=dict)

    def edit_for(self, nfo_path: str) -> Optional[NfoEdit]:
        edit = self.edits.get(nfo_path)
        return edit if edit and edit.changes else None

    def _owner_of(self, nfo_path: str) -> Optional[ItemKey]:
        target = self.target
        if nfo_path == target.nfo_path:
            return target.key
        if nfo_path == target.parent_nfo_path and target.item_kind == SEASON:  # a season's tvshow.nfo
            provider_id, _, category_id, item_id = target.key
            return (provider_id, SERIES, category_id, item_id.split("/", 1)[0])
        return None

    def _edit(self, nfo_path: str, kind: NfoKind) -> NfoEdit:
        edit = self.edits.get(nfo_path)
        if edit is None:
            seed = self.target.title if nfo_path == self.target.nfo_path else None
            edit = self.edits[nfo_path] = NfoEdit(nfo_path, kind, seed, owner_key=self._owner_of(nfo_path))
        return edit


class ArtworkManager:
    def __init__(self, db: Database, settings: Settings, library_root: str,
                 downloader: Optional[ArtworkDownloader] = None, stats: Optional[ArtworkStats] = None,
                 private_fetch: Optional[PrivateFetch] = None) -> None:
        self.repo = ArtworkRepository(db)
        self.nfo_files = NfoFileRepository(db)
        #: ``(plugin_id, download_ref) -> ArtworkPayload | None``: authenticated downloads a
        #: plugin session offers for local mode (see ``Artwork.download_ref``).
        self.private_fetch = private_fetch
        self.settings = settings
        self.library_root = library_root or ""
        self._downloader = downloader
        self._own_downloader = downloader is None
        self.stats = stats or ArtworkStats()

    @property
    def downloader(self) -> ArtworkDownloader:
        if self._downloader is None:
            self._downloader = ArtworkDownloader.from_settings(self.settings)
        return self._downloader

    def close(self) -> None:
        if self._own_downloader and self._downloader is not None:
            self._downloader.close()

    @property
    def mode(self) -> str:
        return self.settings.artwork_mode

    # -- selections ---------------------------------------------------------------------------------
    def stored_selection(self, target: ArtworkTarget) -> dict[Slot, Artwork]:
        result: dict[Slot, Artwork] = {}
        for slot in self.repo.slots(target.key):
            art_type = ArtworkType(slot.artwork_type)
            season = target.season if art_type is ArtworkType.SEASON_POSTER else None
            result[(art_type, season)] = Artwork(art_type, slot.source_url, language=slot.language, width=slot.width,
                                                 height=slot.height, season_number=season,
                                                 source_plugin=slot.source_plugin,
                                                 source_remote_id=slot.source_remote_id)
        return result

    def was_evaluated(self, target: ArtworkTarget) -> bool:
        return self.repo.was_evaluated(target.key)

    def mark_evaluated(self, target: ArtworkTarget) -> None:
        self.repo.mark_evaluated(target.key)

    # -- the two phases ------------------------------------------------------------------------------
    def process(self, target: ArtworkTarget, selections: Optional[dict[Slot, Artwork]] = None,
                force: bool = False) -> None:
        """Prepare and complete in one go (used by reconciliation)."""
        self.complete(self.prepare(target, selections, force))

    def prepare(self, target: ArtworkTarget, selections: Optional[dict[Slot, Artwork]] = None,
                force: bool = False, retire: frozenset[ArtworkType] = frozenset()) -> ItemPlan:
        """Persist new selections and bring every slot of ``target`` in line with the mode.

        Local files are downloaded and written here (before any NFO reference is removed);
        NFO edits and deletions are planned for :meth:`complete`. ``retire``: artwork types
        whose stored selection is known to be wrong (it came from a replaced match) and has
        no new winner; their owned files and NFO references are removed.
        """
        plan = ItemPlan(target)
        allowed = TYPES_BY_KIND.get(target.item_kind, ())
        for (art_type, _season), art in (selections or {}).items():
            if art_type not in allowed or not (art.url or "").strip():
                continue
            self.repo.select(target.key, art_type.value, art.url.strip(), art.source_plugin, art.source_remote_id,
                             art.language, art.width, art.height)
            if art.download_ref and art.source_plugin:
                plan.private[art_type.value] = (art.source_plugin, art.url.strip(), art.download_ref)
        selected = {art_type for art_type, _ in (selections or {})}
        for slot in self.repo.slots(target.key):
            self.stats.considered += 1
            try:
                if ArtworkType(slot.artwork_type) in retire - selected and self.mode in (LOCAL, REMOTE):
                    self._retire(plan, slot)
                    continue
                self._plan_slot(plan, slot, force)
            except Exception as exc:  # one failing slot never stops the item or the job
                self._fail(slot, f"{target.label} {slot.artwork_type}: {exc}")
                log.exception("Artwork handling failed for %s %s", target.label, slot.artwork_type)
        return plan

    def complete(self, plan: ItemPlan, written: Optional[dict[str, bool]] = None) -> None:
        """Write NFO edits the caller did not write, verify all of them, then clean up.

        ``written`` maps NFO paths the caller already saved (with the plan's edits applied)
        to whether that write succeeded.
        """
        written = written or {}
        ok: dict[str, bool] = {}
        for path, edit in plan.edits.items():
            if not edit.changes:
                ok[path] = True
                continue
            if path in written:
                success = written[path] and self._verify(edit)
            else:
                success = self._write(edit)
            ok[path] = success
            if success:
                self._record_edit(edit)
            else:
                self.stats.error(f"{plan.target.label}: NFO could not be updated ({os.path.basename(path)}); "
                                 "previous artwork kept")
        for action in plan.post:
            try:
                if all(ok.get(p, True) for p in action.requires):
                    action.run()
                else:
                    action.blocked()
            except Exception as exc:
                self.stats.error(f"{plan.target.label}: artwork clean-up failed: {exc}")
                log.exception("Artwork clean-up failed for %s", plan.target.label)

    # -- NFO edits ------------------------------------------------------------------------------------
    def _write(self, edit: NfoEdit) -> bool:
        seed = NfoSeed(title=edit.seed_title) if edit.seed_title else None
        try:
            owner = NfoOwner(self.nfo_files, edit.owner_key) if edit.owner_key else None
            update_nfo(edit.nfo_path, edit.nfo_kind, edit.apply, seed, owner)
        except (NfoError, OSError) as exc:
            log.warning("Artwork NFO update failed for %s: %s", edit.nfo_path, exc)
            return False
        return self._verify(edit)

    def _verify(self, edit: NfoEdit) -> bool:
        try:
            return edit.verify(NfoDocument.load(edit.nfo_path, edit.nfo_kind))
        except (NfoError, OSError):
            return False

    def _record_edit(self, edit: NfoEdit) -> None:
        for loc, url, ref_id in edit.removes:
            self.repo.forget_ref(ref_id)
            self.stats.nfo_refs_removed += 1
        for loc, url, slot_id, plugin in edit.adds:
            self.repo.record_ref(slot_id, edit.nfo_path, edit.nfo_kind.value, loc.key, url, plugin)
            self.stats.nfo_urls_written += 1

    # -- per slot -------------------------------------------------------------------------------------
    def _fail(self, slot: SlotRecord, message: str) -> None:
        self.stats.error(message)
        self.repo.set_status(slot.id, ERROR, redact(message)[:500], keep_mode=True)

    def _plan_slot(self, plan: ItemPlan, slot: SlotRecord, force: bool) -> None:
        art_type = ArtworkType(slot.artwork_type)
        if self.mode not in (LOCAL, REMOTE) or art_type not in enabled_types(self.settings, plan.target.item_kind):
            # Disabled: stop adding or changing artwork; existing artwork is left as it is.
            self.repo.set_status(slot.id, DISABLED, None, keep_mode=True)
            self.stats.skipped += 1
            return
        if self.mode == LOCAL:
            self._plan_local(plan, slot, art_type, force)
        else:
            self._plan_remote(plan, slot, art_type)

    def _retire(self, plan: ItemPlan, slot: SlotRecord) -> None:
        """Remove a wrong selection: owned NFO references first, then (once those NFOs are
        written and verified) owned files, then the slot. User artwork is never touched."""
        target = plan.target
        art_type = ArtworkType(slot.artwork_type)
        requires = []
        for ref in self.repo.refs(slot.id):
            plan._edit(ref.nfo_path, NfoKind(ref.nfo_kind)).removes.append(
                (RefLocation.from_key(ref.location), ref.url, ref.id))
            requires.append(ref.nfo_path)

        def run() -> None:
            for record in self._refresh_files(target, slot, art_type):
                if record.status == MANAGED:
                    self._delete_managed(target, art_type, record)
            if self.repo.files(slot.id) or self.repo.refs(slot.id):
                self.repo.set_status(slot.id, MODIFIED, "artwork from a replaced match was changed outside the "
                                                        "application; kept", keep_mode=True)
            else:
                self.repo.delete_slot(slot.id)

        def blocked() -> None:
            self._fail(slot, f"{target.label}: {art_type.value} NFO reference could not be removed; artwork kept")

        plan.post.append(PostAction(tuple(requires), run, blocked))

    # -- local mode -----------------------------------------------------------------------------------
    def _refresh_files(self, target: ArtworkTarget, slot: SlotRecord, art_type: ArtworkType) -> list[FileRecord]:
        """Check recorded files against the disk: follow moves, forget vanished files, flag edits."""
        current: list[FileRecord] = []
        names = all_local_names(target, art_type)
        for record in self.repo.files(slot.id):
            path = record.local_path
            if not atomic.is_file(path):
                moved = self._find_moved(record, names)
                if moved is None:
                    self.repo.forget_file(record.id)  # a record alone is no proof the file exists
                    continue
                self.repo.move_file(record.id, moved)
                record.local_path = path = moved
            if record.status == MANAGED and file_hash(path) != record.file_hash:
                self.repo.set_file_status(record.id, MODIFIED_EXTERNALLY)
                record.status = MODIFIED_EXTERNALLY
                self.stats.warn(f"{target.label}: {os.path.basename(path)} was changed outside the application; "
                                "it is protected and will not be replaced or deleted")
            current.append(record)
        return current

    def _find_moved(self, record: FileRecord, names: list[LocalName]) -> Optional[str]:
        """The library moved (renamed item, new root): find the same file at its new canonical name."""
        for name in names:
            if name.role != record.role:
                continue
            for candidate in existing_variants(name.base):
                if file_hash(candidate) == record.file_hash:
                    return candidate
        return None

    def _plan_local(self, plan: ItemPlan, slot: SlotRecord, art_type: ArtworkType, force: bool) -> None:
        target = plan.target
        records = self._refresh_files(target, slot, art_type)
        desired = local_names(target, art_type, self.settings.artwork_aliases)
        if not desired:
            self.repo.set_status(slot.id, DISABLED, "no local file name for this item", keep_mode=True)
            self.stats.skipped += 1
            return

        def records_at(base: str) -> list[FileRecord]:
            return [r for r in records if _same_path(_base(r.local_path), base)]

        needs: list[LocalName] = []
        kept = modified = False
        for name in desired:
            on_disk = existing_variants(name.base)
            ours = records_at(name.base)
            owned_paths = [r.local_path for r in ours]
            unmanaged = [p for p in on_disk if not any(_same_path(p, o) for o in owned_paths)]
            if unmanaged:
                # User or third-party artwork: never overwritten, never deleted, nothing added beside it.
                self.repo.set_status(slot.id, EXTERNAL, f"existing artwork kept: {os.path.basename(unmanaged[0])}",
                                     keep_mode=True)
                self.stats.skipped += 1
                return
            if not ours:
                needs.append(name)
                continue
            record = ours[0]
            if record.status == MODIFIED_EXTERNALLY:
                if force:
                    needs.append(name)
                else:
                    modified = True
            elif record.source_url != slot.source_url:
                if force or self.settings.artwork_existing == "replace_managed":
                    needs.append(name)
                else:
                    kept = True
        if modified:
            self.repo.set_status(slot.id, MODIFIED, "a managed file was changed outside the application; "
                                                    "use force replace to overwrite it", keep_mode=True)
            self.stats.skipped += 1
            return

        changed = False
        if needs:
            image = self._obtain(slot, records, keep_existing=kept, private=plan.private.get(slot.artwork_type))
            if image is None:
                return  # error recorded; NFO references and existing files stay as they are
            data, image_format, source_url = image
            extension = FORMAT_EXTENSIONS[image_format]
            digest = hashlib.sha256(data).hexdigest()
            for name in needs:
                path = name.base + extension
                atomic.atomic_write_bytes(path, data)
                if file_hash(path) != digest:
                    raise OSError(f"verification of {os.path.basename(path)} failed")
                self.repo.record_file(slot.id, name.role, path, digest, len(data), source_url, slot.source_plugin)
                for old in records_at(name.base):  # same name, other extension: the replaced managed file
                    if not _same_path(old.local_path, path):
                        self._delete_managed(target, art_type, old)
            changed = True

        # Aliases switched off: managed alias copies are no longer wanted.
        wanted = {os.path.normcase(os.path.abspath(n.base)) for n in desired}
        for record in self.repo.files(slot.id):
            if os.path.normcase(os.path.abspath(_base(record.local_path))) not in wanted and record.status == MANAGED:
                changed = self._delete_managed(target, art_type, record) or changed

        # The local file is authoritative now: drop managed remote references (unless kept on purpose).
        if not self.settings.artwork_keep_previous:
            for ref in self.repo.refs(slot.id):
                plan._edit(ref.nfo_path, NfoKind(ref.nfo_kind)).removes.append(
                    (RefLocation.from_key(ref.location), ref.url, ref.id))
                changed = True
        if kept:
            self.repo.set_status(slot.id, KEPT, "a newer candidate is available; the existing managed file is kept "
                                                "(Existing local artwork: Keep existing)", mode=LOCAL)
        else:
            self.repo.set_status(slot.id, OK, None, mode=LOCAL)
        if not changed:
            self.stats.unchanged += 1

    def _obtain(self, slot: SlotRecord, records: list[FileRecord], keep_existing: bool,
                private: Optional[tuple[str, str, str]] = None) -> Optional[tuple[bytes, str, str]]:
        """Image bytes, format and source URL: an intact managed copy when possible, else one download.

        With ``keep_existing`` (a newer candidate exists but the managed file is kept) any
        intact managed copy is reused, so compatibility aliases always match the kept file.
        A selected candidate with an authenticated download is tried first; the public URL is
        the fallback and is what gets recorded as the file's source.
        """
        for record in records:
            if record.status != MANAGED or (record.source_url != slot.source_url and not keep_existing):
                continue
            try:
                data = atomic.read_bytes(record.local_path)
            except OSError:
                continue
            image_format = detect_format(data)
            if image_format and hashlib.sha256(data).hexdigest() == record.file_hash:
                return data, image_format, record.source_url
        image_bytes = self._private(slot, private)
        if image_bytes is not None:
            self.stats.downloaded += 1
            return image_bytes[0], image_bytes[1], slot.source_url
        try:
            image = self.downloader.fetch(slot.source_url)
        except ArtworkDownloadError as exc:
            self._fail(slot, f"{slot.artwork_type} download failed: {exc} ({redact(slot.source_url)})")
            return None
        self.stats.downloaded += 1
        return image.data, image.format, slot.source_url

    def _private(self, slot: SlotRecord, private: Optional[tuple[str, str, str]]) -> Optional[tuple[bytes, str]]:
        """A validated image from the plugin's authenticated download, or ``None`` (use the public URL)."""
        if private is None or self.private_fetch is None:
            return None
        plugin_id, public_url, download_ref = private
        if plugin_id != slot.source_plugin or public_url != slot.source_url:
            return None
        try:
            payload = self.private_fetch(plugin_id, download_ref)
            if payload is None:
                return None
            return payload.data, validate_image(payload.data, payload.content_type or "")
        except ArtworkDownloadError as exc:
            log.info("%s: authenticated %s download rejected (%s); using the public image", plugin_id,
                     slot.artwork_type, exc)
        except Exception as exc:
            log.warning("%s: authenticated %s download failed (%s); using the public image", plugin_id,
                        slot.artwork_type, redact(exc))
        return None

    # -- remote mode ----------------------------------------------------------------------------------
    def _plan_remote(self, plan: ItemPlan, slot: SlotRecord, art_type: ArtworkType) -> None:
        target = plan.target
        locations = nfo_locations(target, art_type)
        if not locations:
            self.repo.set_status(slot.id, DISABLED, "this artwork type has no NFO representation", keep_mode=True)
            self.stats.skipped += 1
            return
        refs = self.repo.refs(slot.id)
        requires: list[str] = []
        primary_ours = False
        changed = False
        for index, (nfo_path, kind, loc) in enumerate(locations):
            try:
                doc = NfoDocument.load(nfo_path, kind) if atomic.is_file(nfo_path) else None
            except NfoError as exc:
                self._fail(slot, f"{target.label}: NFO cannot be read, artwork left unchanged: {exc}")
                return
            present = find_urls(doc, loc) if doc is not None else []
            ours = [r for r in refs if r.location == loc.key and _same_path(r.nfo_path, nfo_path)]
            for ref in [r for r in refs if r.location == loc.key and not _same_path(r.nfo_path, nfo_path)
                        and r.url in present and NfoKind(r.nfo_kind) is kind]:
                self.repo.move_ref(ref.id, nfo_path)  # the item (and its NFO) moved
                ours.append(ref)
            owned = {r.url for r in ours} | {r.url for r in self.repo.refs_at(nfo_path, loc.key)}
            unmanaged = [u for u in present if u not in owned]
            if unmanaged:
                continue  # somebody else's artwork reference: keep it, add nothing beside it
            edit = plan._edit(nfo_path, kind)
            for ref in ours:
                if ref.url != slot.source_url:
                    edit.removes.append((loc, ref.url, ref.id))
                    changed = True
            if slot.source_url not in present or not any(r.url == slot.source_url for r in ours):
                edit.adds.append((loc, slot.source_url, slot.id, slot.source_plugin))
                changed = True
            requires.append(nfo_path)
            if index == 0:
                primary_ours = True
        if not requires:
            self.repo.set_status(slot.id, EXTERNAL, "existing NFO artwork reference kept", keep_mode=True)
            self.stats.skipped += 1
            return

        def run() -> None:
            removed = False
            if primary_ours and not self.settings.artwork_keep_previous:
                for record in self.repo.files(slot.id):
                    removed = self._delete_managed(target, art_type, record) or removed
            self.repo.set_status(slot.id, OK, None, mode=REMOTE)
            if not changed and not removed:
                self.stats.unchanged += 1

        def blocked() -> None:
            self._fail(slot, f"{target.label}: {art_type.value} NFO reference could not be written; "
                             "local artwork kept")

        plan.post.append(PostAction(tuple(requires), run, blocked))

    # -- permanent purge of a missing item ------------------------------------------------------------
    def purge(self, target: ArtworkTarget) -> bool:
        """Delete the item's application-owned local artwork (the item is being purged).

        Exactly the checks of a normal replacement apply: a file is deleted only when its
        ownership record, path and hash prove it is the application's. Everything else (user
        artwork, files changed outside the application) is kept. Returns False when an owned
        file could not be deleted, so the caller keeps the item's state and retries later.
        """
        ok = True
        for slot in self.repo.slots(target.key):
            try:
                art_type = ArtworkType(slot.artwork_type)
            except ValueError:
                continue
            for record in self._refresh_files(target, slot, art_type):  # follows moves, flags edits
                if record.status != MANAGED:
                    continue
                path = record.local_path
                reason = self._unsafe(target, art_type, record)
                if reason:
                    self.stats.warn(f"{target.label}: not deleting {os.path.basename(path)}: {reason}")
                    continue
                try:
                    os.remove(atomic.fs_path(path))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    self.stats.error(f"{target.label}: artwork {os.path.basename(path)} could not be deleted: {exc}")
                    ok = False
                    continue
                self.repo.forget_file(record.id)
                self.stats.local_removed += 1
                log.debug("Purged managed artwork %s", path)
        return ok

    # -- safe deletion --------------------------------------------------------------------------------
    def _delete_managed(self, target: ArtworkTarget, art_type: ArtworkType, record: FileRecord) -> bool:
        """Delete one application-managed artwork file after every safety check passes."""
        path = record.local_path
        reason = self._unsafe(target, art_type, record)
        if reason:
            self.stats.warn(f"{target.label}: not deleting {os.path.basename(path)}: {reason}")
            return False
        if not atomic.is_file(path):
            self.repo.forget_file(record.id)
            return False
        if file_hash(path) != record.file_hash:
            self.repo.set_file_status(record.id, MODIFIED_EXTERNALLY)
            self.stats.warn(f"{target.label}: {os.path.basename(path)} was changed outside the application; kept")
            return False
        try:
            os.remove(atomic.fs_path(path))
        except OSError as exc:
            self.stats.error(f"{target.label}: local artwork {os.path.basename(path)} could not be deleted: {exc}")
            return False
        self.repo.forget_file(record.id)
        self.stats.local_removed += 1
        log.debug("Removed managed artwork %s", path)
        return True

    def _unsafe(self, target: ArtworkTarget, art_type: ArtworkType, record: FileRecord) -> Optional[str]:
        """Why deleting ``record`` would be unsafe (``None`` = safe)."""
        path = record.local_path
        if record.status != MANAGED:
            return "not managed by this application"
        slot = self.repo.get_slot(record.slot_id)
        if slot is None or slot.key != target.key or slot.artwork_type != art_type.value:
            return "ownership record does not belong to this item"
        if not self.library_root:
            return "library root unknown"
        root = os.path.normcase(os.path.realpath(self.library_root))
        real = os.path.realpath(path)
        if not os.path.normcase(real).startswith(root.rstrip(os.sep) + os.sep):
            return "outside the library folder"
        if not _same_path(os.path.dirname(real), os.path.realpath(target.folder)):
            return "not in the item's folder"
        if os.path.islink(path):
            return "is a symbolic link"
        if os.path.splitext(path)[1].lower() not in FORMAT_EXTENSIONS.values():
            return "not an artwork file"
        if not any(_same_path(_base(path), n.base) for n in all_local_names(target, art_type)):
            return "not a canonical artwork file name"
        return None


KIND_ORDER = (MOVIE, SERIES, SEASON, EPISODE)
