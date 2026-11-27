from __future__ import annotations

import time
import uuid
from pathlib import Path

from config.settings import (
    CONFIG_FILE,
    COVER_DIR,
    DB_FILE,
    LIBRARY_FILE,
    current_db_file,
)
from core.database import Database, migrate_legacy
from core.metadata import build_track_from_file
from core.models import (
    DEFAULT_PLAYLIST_ID,
    DEFAULT_PLAYLIST_NAME,
    Playlist,
    Track,
)

# Set once the pre-playlist library has been filed into the default bucket.
# Without the flag every launch would re-adopt tracks the user deliberately
# pulled out of their playlists.
_ORPHAN_MIGRATION_KEY = "library_filed_into_default"


def _path_key(path: str) -> str:
    return str(Path(path).resolve()).casefold()


class Library:
    """Media library backed by SQLite, mirrored in memory for fast reads."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        # None means "whatever the current location pointer says", so a
        # database relocated in Settings is picked up without a code change.
        self.db = Database(db_path or current_db_file())
        self.tracks: dict[str, Track] = {}
        self.playlists: dict[str, Playlist] = {}
        self.load()

    def close(self) -> None:
        self.db.close()

    def load(self) -> None:
        migrate_legacy(self.db, LIBRARY_FILE, CONFIG_FILE)
        self.tracks, self.playlists = self.db.load_library()
        self.ensure_default_playlist()
        self._file_orphans_once()

    def save(self) -> None:
        self.db.replace_library(self.tracks.values(), self.playlists.values())

    # ---------- Playlists ----------

    def ensure_default_playlist(self) -> Playlist:
        """Return the default bucket, creating it the first time."""
        playlist = self.playlists.get(DEFAULT_PLAYLIST_ID)
        if playlist is not None:
            return playlist
        # Adopt a hand-made playlist already spelled "默认歌单" so upgrading
        # never leaves two rows with the same caption.
        existing = next(
            (
                candidate
                for candidate in self.playlists.values()
                if candidate.name == DEFAULT_PLAYLIST_NAME
            ),
            None,
        )
        if existing is not None:
            del self.playlists[existing.id]
            existing.id = DEFAULT_PLAYLIST_ID
            self.playlists[existing.id] = existing
            self.save()
            return existing
        playlist = Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME)
        self.playlists[playlist.id] = playlist
        self.save()
        return playlist

    def default_playlist_id(self) -> str:
        return self.ensure_default_playlist().id

    def ordered_playlists(self) -> list[Playlist]:
        """Default bucket first, the rest in creation order."""
        ordered = sorted(self.playlists.values(), key=lambda item: item.created_at)
        ordered.sort(key=lambda item: item.id != DEFAULT_PLAYLIST_ID)
        return ordered

    def playlists_containing(self, track_id: str) -> list[str]:
        return [
            playlist.id
            for playlist in self.playlists.values()
            if track_id in playlist.track_ids
        ]

    def _file_orphans_once(self) -> None:
        """Move a pre-playlist library into the default bucket, exactly once.

        Until playlists became the only view, tracks could live in the library
        without belonging to any list. Such tracks would become unreachable, so
        they are adopted on the first launch after the change and then left
        alone: pulling a track out of its last playlist stays respected.
        """
        if self.db.get_setting(_ORPHAN_MIGRATION_KEY, False):
            return
        default = self.ensure_default_playlist()
        filed = 0
        for track in self.tracks.values():
            if not self.playlists_containing(track.id):
                default.track_ids.append(track.id)
                filed += 1
        if filed:
            self.save()
        self.db.set_setting(_ORPHAN_MIGRATION_KEY, True)

    def all_tracks(self) -> list[Track]:
        return sorted(self.tracks.values(), key=lambda track: track.added_at, reverse=True)

    def tracks_for_playlist(self, playlist_id: str) -> list[Track]:
        playlist = self.playlists.get(playlist_id)
        if playlist is None:
            return []
        return [self.tracks[track_id] for track_id in playlist.track_ids if track_id in self.tracks]

    def add_track(self, track: Track) -> bool:
        if any(_path_key(existing.path) == _path_key(track.path) for existing in self.tracks.values()):
            return False
        self.tracks[track.id] = track
        self.save()
        return True

    def add_tracks(self, paths: list[str] | list[Path]) -> tuple[list[Track], int]:
        added: list[Track] = []
        skipped = 0
        for raw_path in paths:
            path = Path(raw_path)
            if not path.is_file():
                skipped += 1
                continue
            if any(_path_key(existing.path) == _path_key(path) for existing in self.tracks.values()):
                skipped += 1
                continue
            track = build_track_from_file(path)
            self.tracks[track.id] = track
            added.append(track)
        if added:
            self.save()
        return added, skipped

    def remove_track(self, track_id: str) -> None:
        self.tracks.pop(track_id, None)
        for playlist in self.playlists.values():
            playlist.track_ids = [value for value in playlist.track_ids if value != track_id]
        self.save()

    def delete_tracks_with_files(self, track_ids: list[str]) -> tuple[int, list[str]]:
        """Remove tracks from library/playlists and delete their audio files.

        Returns (library_removed_count, failure_messages). The library entry is
        always removed, even when the file itself cannot be deleted; the
        caller reports those cases so the user can clean them up manually.
        Cover art is deleted only when no remaining track references it.
        Missing audio files are treated as already removed (not a failure).
        """
        failures: list[str] = []
        deleted = 0
        remaining_covers = {
            track.cover_file
            for track_id, track in self.tracks.items()
            if track_id not in track_ids and track.cover_file
        }
        for track_id in track_ids:
            track = self.tracks.get(track_id)
            if track is None:
                continue
            audio_path = Path(track.path)
            file_removed = True
            if audio_path.is_file():
                # The media backend may keep a handle briefly after stop().
                # Retry a few times before reporting a real failure.
                file_removed = False
                last_error: OSError | None = None
                for _ in range(5):
                    try:
                        audio_path.unlink()
                        file_removed = True
                        break
                    except OSError as exc:
                        last_error = exc
                        time.sleep(0.15)
                if not file_removed:
                    failures.append(f"《{track.title}》音频文件删除失败：{last_error}")
            if file_removed and track.cover_file and track.cover_file not in remaining_covers:
                cover_path = COVER_DIR / track.cover_file
                try:
                    if cover_path.is_file():
                        cover_path.unlink()
                except OSError:
                    pass  # Cover cleanup is best-effort; audio removal already succeeded.
            # Remove the library entry regardless of the file result: the user
            # asked to delete the track, and keeping it would leave a playable
            # row pointing at a file that may no longer exist.
            self.remove_track(track_id)
            deleted += 1
        return deleted, failures

    def create_playlist(self, name: str) -> Playlist:
        playlist = Playlist(id=uuid.uuid4().hex, name=name.strip() or "未命名歌单")
        self.playlists[playlist.id] = playlist
        self.save()
        return playlist

    def rename_playlist(self, playlist_id: str, name: str) -> bool:
        playlist = self.playlists.get(playlist_id)
        if playlist is None or not name.strip():
            return False
        # The default bucket is addressed by name in the copy ("未指定歌单的
        # 音乐会进入默认歌单"), so it keeps its caption.
        if playlist_id == DEFAULT_PLAYLIST_ID:
            return False
        playlist.name = name.strip()
        self.save()
        return True

    def delete_playlist(self, playlist_id: str) -> list[str]:
        """Drop a playlist and wipe its songs from the database.

        A playlist is the only place its songs live, so deleting one takes the
        tracks out of the library too (audio files stay on disk). A song that
        also sits in another playlist survives there. Returns the ids of the
        tracks that were removed, so callers can purge the player queue.
        """
        if playlist_id == DEFAULT_PLAYLIST_ID:
            return []
        playlist = self.playlists.get(playlist_id)
        if playlist is None:
            return []
        others = {
            track_id
            for pid, other in self.playlists.items()
            if pid != playlist_id
            for track_id in other.track_ids
        }
        removed_ids = [
            track_id for track_id in playlist.track_ids if track_id not in others
        ]
        self.playlists.pop(playlist_id, None)
        for track_id in removed_ids:
            self.tracks.pop(track_id, None)
        self.save()
        self.db.forget_playback_refs(playlist_id, removed_ids)
        return removed_ids

    def add_to_playlist(self, playlist_id: str, track_ids: list[str]) -> int:
        playlist = self.playlists.get(playlist_id)
        if playlist is None:
            return 0
        count = 0
        for track_id in track_ids:
            if track_id in self.tracks and track_id not in playlist.track_ids:
                playlist.track_ids.append(track_id)
                count += 1
        if count:
            self.save()
        return count

    def remove_from_playlist(self, playlist_id: str, track_id: str) -> None:
        playlist = self.playlists.get(playlist_id)
        if playlist is None:
            return
        playlist.track_ids = [value for value in playlist.track_ids if value != track_id]
        self.save()
