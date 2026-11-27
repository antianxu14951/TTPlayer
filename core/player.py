from __future__ import annotations

import random
from enum import Enum

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from config.settings import load_config, load_playback_state, save_config, save_playback_state
from core.models import Track


def _device_id(device: object) -> str:
    """Encode a QAudioDevice id as a stable hex string for persistence."""
    try:
        return bytes(device.id()).hex()  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return ""


def _audio_probe_available() -> bool:
    """Headless Qt platforms block when probing the audio backend."""
    try:
        return QGuiApplication.platformName() not in {"offscreen", "minimal"}
    except Exception:
        return False


class PlaybackMode(str, Enum):
    LIST_LOOP = "list_loop"
    SHUFFLE = "shuffle"
    SINGLE_LOOP = "single_loop"


MODE_LABELS = {
    PlaybackMode.LIST_LOOP: "列表循环",
    PlaybackMode.SHUFFLE: "随机播放",
    PlaybackMode.SINGLE_LOOP: "单曲循环",
}


class PlayerController(QObject):
    trackChanged = Signal(object)
    stateChanged = Signal(bool)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    modeChanged = Signal(object)
    queueChanged = Signal()
    errorOccurred = Signal(str)
    devicesChanged = Signal()
    audioDeviceChanged = Signal(str)
    # Audition ("试听") borrows the shared transport but keeps its own temporary
    # queue. These signals let the search page resolve the next stream without
    # the controller knowing anything about LX sources.
    previewChanged = Signal(bool)
    previewNextRequested = Signal()
    previewPreviousRequested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        config = load_config()
        state = load_playback_state()
        # The last volume the user actually adjusted lives in playback_state;
        # fall back to the config value for fresh installs.
        restored_volume = float(state.get("volume", config.get("volume", 0.7)))
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(restored_volume)
        # Keep an instance alive to receive the static device-change signal.
        self._devices = QMediaDevices(self)

        self._queue: list[Track] = []
        self._index = -1
        self._history: list[int] = []
        self._played_cycle: set[int] = set()
        self._skip_end_once = False
        # A seek requested before the media finished loading is ignored by the
        # backend, so it is queued here and applied on LoadedMedia.
        self._pending_position: int | None = None
        # Preview session: a temporary queue that never touches ``_queue``.
        self._previewing = False
        self._preview_tracks: list[Track] = []
        self._preview_index = -1
        self._preview_urls: dict[str, str] = {}
        self._preview_resume: dict[str, object] = {}
        try:
            self._mode = PlaybackMode(str(config.get("playback_mode", "list_loop")))
        except ValueError:
            self._mode = PlaybackMode.LIST_LOOP

        # Restore the previously selected output device when it still exists.
        self._device_id = str(load_playback_state().get("audio_device_id", ""))
        if self._device_id and self._find_device(self._device_id) is not None:
            self._audio.setDevice(self._find_device(self._device_id))
        self._devices.audioOutputsChanged.connect(self._on_devices_changed)

        self._player.positionChanged.connect(self.positionChanged.emit)
        self._player.durationChanged.connect(self.durationChanged.emit)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_media_error)

    @property
    def mode(self) -> PlaybackMode:
        return self._mode

    @property
    def current_track(self) -> Track | None:
        if self._previewing:
            if 0 <= self._preview_index < len(self._preview_tracks):
                return self._preview_tracks[self._preview_index]
            return None
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    @property
    def is_previewing(self) -> bool:
        """True while an audition owns the transport but not the real queue."""
        return self._previewing

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def queue(self) -> list[Track]:
        """A copy of the real queue, unaffected by any audition."""
        return list(self._queue)

    @property
    def queue_position(self) -> int:
        """1-based position of the playing entry, 0 when nothing is loaded."""
        if self._previewing:
            return 0
        if 0 <= self._index < len(self._queue):
            return self._index + 1
        return 0

    def play_tracks(self, tracks: list[Track], start_index: int = 0) -> None:
        if not tracks:
            self.errorOccurred.emit("当前列表没有可播放的音乐")
            return
        if self._previewing:
            self.exit_preview(restore=False)
        self._queue = list(tracks)
        self._index = -1
        self._history.clear()
        self._played_cycle.clear()
        self.queueChanged.emit()
        self._goto(max(0, min(start_index, len(self._queue) - 1)))

    def enqueue(self, tracks: list[Track]) -> int:
        """Append tracks to the active queue without touching playback.

        The search page uses this for "添加到播放列表": a downloaded song waits
        at the end of the queue instead of cutting off whatever is playing.
        With an idle queue the first track is merely loaded (paused), so the
        transport's play button has something to start.

        While an audition is running the queue is only extended: loading the
        first track would replace the stream the user is listening to.
        """
        known = {track.id for track in self._queue}
        fresh = [track for track in tracks if track.id not in known]
        if not fresh:
            return 0
        if not self._queue:
            self._queue = list(fresh)
            self._index = 0
            self._history.clear()
            self._played_cycle = {0}
            self._skip_end_once = True
            if not self._previewing:
                self._player.setSource(QUrl.fromLocalFile(fresh[0].path))
                self.trackChanged.emit(fresh[0])
        else:
            self._queue.extend(fresh)
        self.queueChanged.emit()
        return len(fresh)

    def toggle_play(self) -> None:
        if self.current_track is None:
            self.errorOccurred.emit("请先导入音乐，再开始播放")
            return
        if self.is_playing:
            self._player.pause()
        else:
            self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def next(self) -> None:
        if self._previewing:
            if self._preview_index + 1 < len(self._preview_tracks):
                self.previewNextRequested.emit()
            return
        if not self._queue:
            return
        if self._mode == PlaybackMode.SHUFFLE:
            target = self._shuffle_next()
        else:
            target = (self._index + 1) % len(self._queue)
        self._goto(target)

    def previous(self) -> None:
        if self._previewing:
            if self._preview_index > 0:
                self.previewPreviousRequested.emit()
            return
        if not self._queue:
            return
        if self._mode == PlaybackMode.SHUFFLE and self._history:
            target = self._history.pop()
        elif self._mode == PlaybackMode.SHUFFLE:
            target = self._shuffle_next()
        else:
            target = (self._index - 1) % len(self._queue)
        self._goto(target)

    def play_at(self, index: int) -> None:
        """Start an existing queue entry without rebuilding the queue.

        The "正在播放" view uses this: jumping inside the queue must not throw
        away the rest of the list, and an audition owning the transport is
        dropped first (without restoring, since we are about to load a file).
        """
        if not (0 <= index < len(self._queue)):
            return
        if self._previewing:
            self.exit_preview(restore=False)
        self._goto(index)

    def clear_queue(self) -> None:
        """Empty the playing queue and silence the transport.

        This never touches the library or playlists on disk — it only ends the
        listening session, so the transport shows "未播放" afterwards.
        """
        if self._previewing:
            self.exit_preview(restore=False)
        if not self._queue and self.current_track is None:
            return
        self._queue = []
        self._index = -1
        self._history.clear()
        self._played_cycle.clear()
        self._skip_end_once = True
        self._pending_position = None
        self.stop()
        self._player.setSource(QUrl())
        self.stateChanged.emit(False)
        self.trackChanged.emit(None)
        self.queueChanged.emit()

    # ---------- Audition (preview) ----------

    def prepare_preview(self, tracks: list[Track], index: int) -> None:
        """Put a search result on the transport while its stream resolves.

        The real queue is snapshotted once, the first time an audition starts.
        Switching between results only replaces the temporary queue, so the
        library's "now playing" list is never polluted.
        """
        if not tracks or not (0 <= index < len(tracks)):
            return
        self._enter_preview(tracks, index)
        self._skip_end_once = True
        self._pending_position = None
        self._player.stop()
        self._player.setSource(QUrl())
        # The bottom bar must not keep the previous song's length while a new
        # stream is being resolved.
        self.positionChanged.emit(0)
        self.durationChanged.emit(0)

    def start_preview(self, tracks: list[Track], index: int, url: str) -> None:
        """Audition a resolved stream through the shared transport."""
        if not tracks or not (0 <= index < len(tracks)) or not url:
            return
        self._enter_preview(tracks, index)
        self._preview_urls[tracks[index].id] = url
        self._skip_end_once = True
        self._pending_position = None
        self._player.setSource(QUrl(url))
        self._player.play()

    def set_preview_url(self, track_id: str, url: str) -> None:
        """Hand a resolved stream to the audition that is waiting for it."""
        if not url:
            return
        self._preview_urls[str(track_id)] = url
        if not self._previewing:
            return
        track = self.current_track
        if track is None or track.id != str(track_id):
            return
        self._skip_end_once = True
        self._player.setSource(QUrl(url))
        self._player.play()

    def exit_preview(self, restore: bool = True, resume: bool | None = None) -> None:
        """Leave the audition and hand the transport back to the real queue.

        ``restore`` seeks back to where the library was. ``resume`` overrides
        whether playback continues: ``None`` follows the snapshot (resume only
        if it was playing), ``False`` always returns paused.
        """
        if not self._previewing:
            return
        snapshot = self._preview_resume
        self._previewing = False
        self._preview_tracks = []
        self._preview_index = -1
        self._preview_urls.clear()
        self._preview_resume = {}
        self.previewChanged.emit(False)
        if not restore:
            self.queueChanged.emit()
            return

        self._skip_end_once = True
        track = (
            self._queue[self._index]
            if 0 <= self._index < len(self._queue)
            else None
        )
        if track is None:
            self._player.stop()
            self._player.setSource(QUrl())
            self.trackChanged.emit(None)
        else:
            position = int(snapshot.get("position") or 0)
            self._pending_position = position if position > 0 else None
            self._player.setSource(QUrl.fromLocalFile(track.path))
            should_play = bool(snapshot.get("playing"))
            if resume is not None:
                should_play = bool(resume)
            if should_play:
                self._player.play()
            self.trackChanged.emit(track)
        self.queueChanged.emit()

    def _enter_preview(self, tracks: list[Track], index: int) -> None:
        if not self._previewing:
            self._preview_resume = {
                "position": max(0, int(self._player.position())),
                "playing": self.is_playing,
            }
            self._previewing = True
            self.previewChanged.emit(True)
        self._preview_tracks = list(tracks)
        self._preview_index = index
        self.trackChanged.emit(tracks[index])
        self.queueChanged.emit()

    def set_mode(self, mode: PlaybackMode) -> None:
        self._mode = mode
        self._played_cycle = {self._index} if self._index >= 0 else set()
        config = load_config()
        config["playback_mode"] = mode.value
        save_config(config)
        self.modeChanged.emit(mode)

    def set_volume(self, value: float) -> None:
        volume = max(0.0, min(1.0, float(value)))
        self._audio.setVolume(volume)
        save_playback_state({"volume": volume})

    # ---------- Audio output device ----------

    def audio_devices(self) -> list[dict[str, str]]:
        """Enumerate output devices as {id, name, is_default} records."""
        if not _audio_probe_available():
            return []
        return [
            {
                "id": _device_id(device),
                "name": device.description(),
                "is_default": "1" if device.isDefault() else "0",
            }
            for device in QMediaDevices.audioOutputs()
        ]

    def current_device_id(self) -> str:
        """Return the id of the active output device ("" means system default)."""
        return self._device_id

    def current_volume(self) -> float:
        """Current output volume, so secondary audio players can match it."""
        return float(self._audio.volume())

    def output_device(self):  # type: ignore[no-untyped-def]
        """Return the ``QAudioDevice`` in use, so previews share the output."""
        return self._audio.device()

    def _find_device(self, device_id: str):  # type: ignore[no-untyped-def]
        if not device_id or not _audio_probe_available():
            return None
        for device in QMediaDevices.audioOutputs():
            if _device_id(device) == device_id:
                return device
        return None

    def set_audio_device(self, device_id: str) -> bool:
        """Switch playback to the given device id. Empty string restores default."""
        if not device_id:
            default = QMediaDevices.defaultAudioOutput() if _audio_probe_available() else None
            if default is None:
                return False
            self._audio.setDevice(default)
            self._device_id = _device_id(default)
        else:
            device = self._find_device(device_id)
            if device is None:
                return False
            self._audio.setDevice(device)
            self._device_id = device_id
        save_playback_state({"audio_device_id": self._device_id})
        self.audioDeviceChanged.emit(self._device_id)
        return True

    def _on_devices_changed(self) -> None:
        # If the selected device disappeared, fall back to the system default.
        if self._device_id and self._find_device(self._device_id) is None:
            default = QMediaDevices.defaultAudioOutput()
            self._audio.setDevice(default)
            self._device_id = _device_id(default)
            save_playback_state({"audio_device_id": self._device_id})
            self.audioDeviceChanged.emit(self._device_id)
        self.devicesChanged.emit()

    # ---------- Session persistence ----------

    def save_session(
        self, playlist_id: str = "", position_ms: int | None = None
    ) -> None:
        """Persist what the user is currently listening to.

        An audition is deliberately not persisted: it has no library id and
        would overwrite the real "last played" record with a stream URL.
        """
        if self._previewing:
            return
        track = self.current_track
        save_playback_state(
            {
                "playlist_id": playlist_id,
                "track_id": track.id if track is not None else "",
                "position_ms": (
                    max(0, int(position_ms))
                    if position_ms is not None
                    else max(0, self._player.position())
                ),
                "volume": self._audio.volume(),
                "audio_device_id": self._device_id,
            }
        )

    def restore_session(
        self, tracks: list[Track], start_index: int, position_ms: int = 0
    ) -> Track | None:
        """Load a saved queue and seek position WITHOUT auto-playing on startup."""
        if not tracks or not (0 <= start_index < len(tracks)):
            return None
        self._queue = list(tracks)
        self._index = start_index
        self._history.clear()
        self._played_cycle = {start_index}
        self._skip_end_once = True
        track = tracks[start_index]
        self._pending_position = max(0, int(position_ms)) or None
        self._player.setSource(QUrl.fromLocalFile(track.path))
        self.trackChanged.emit(track)
        self.queueChanged.emit()
        return track

    def set_position(self, position_ms: int) -> None:
        self._player.setPosition(max(0, int(position_ms)))

    def stop(self) -> None:
        self._player.stop()

    def release_source(self) -> None:
        """Stop playback first, then detach the media so the file can be deleted.

        Windows keeps an open handle on the file behind the current media
        source. ``stop()`` alone leaves the URL loaded, so ``Path.unlink()``
        would fail with WinError 32. This method performs both steps in order:
        (1) stop the playing track, (2) clear the source, (3) let the backend
        release the handle by draining pending events. ``_skip_end_once``
        prevents the detach from being mistaken for natural end-of-media and
        advancing to the next track.
        """
        self._skip_end_once = True
        self.stop()  # step 1: always stop before any file deletion
        self._player.setSource(QUrl())  # step 2: release the loaded media
        for _ in range(3):  # step 3: allow the backend to close its handle
            QCoreApplication.processEvents()

    def remove_from_queue(
        self, track_ids: list[str], *, already_stopped: bool = False
    ) -> None:
        """Drop tracks from the active queue, keeping playback coherent.

        If the playing track is removed, playback must be stopped before its
        file is deleted (Windows keeps a handle on open media). Callers that
        already stopped it pass ``already_stopped=True``.
        """
        removed = set(track_ids)
        if not removed:
            return
        # Always inspect the real queue: while previewing, ``current_track``
        # points at an audition that is not part of it.
        current = (
            self._queue[self._index]
            if 0 <= self._index < len(self._queue)
            else None
        )
        current_removed = current is not None and current.id in removed
        remaining = [track for track in self._queue if track.id not in removed]
        # History entries are indices into the old queue; after removal they
        # would point at the wrong tracks, so reset them.
        self._history.clear()
        self._played_cycle.clear()
        if current_removed:
            if not already_stopped and not self._previewing:
                self.release_source()
            self._index = -1
            self._queue = remaining
            self._skip_end_once = True
            if not self._previewing:
                self.stateChanged.emit(False)
                self.trackChanged.emit(None)
        elif current is not None:
            new_index = next(
                (index for index, track in enumerate(remaining) if track.id == current.id),
                -1,
            )
            self._index = new_index
            self._queue = remaining
        else:
            self._queue = remaining
            self._index = -1
        self.queueChanged.emit()

    def _shuffle_next(self) -> int:
        candidates = [
            index
            for index in range(len(self._queue))
            if index != self._index and index not in self._played_cycle
        ]
        if not candidates:
            self._played_cycle = {self._index} if self._index >= 0 else set()
            candidates = [index for index in range(len(self._queue)) if index != self._index]
        if not candidates:
            return self._index
        return random.choice(candidates)

    def _goto(self, index: int) -> None:
        if not self._queue or not (0 <= index < len(self._queue)):
            return
        if self._index >= 0:
            self._history.append(self._index)
        self._index = index
        self._played_cycle.add(index)
        track = self._queue[index]
        self._skip_end_once = True
        self._pending_position = None
        self._player.setSource(QUrl.fromLocalFile(track.path))
        self._player.play()
        self.trackChanged.emit(track)
        self.queueChanged.emit()

    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._skip_end_once = False
        self.stateChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            # The backend drops seeks issued before the media is loaded, so the
            # restore position is applied as soon as it is ready.
            if self._pending_position is not None:
                pending = self._pending_position
                self._pending_position = None
                if pending > 0:
                    self._player.setPosition(pending)
            return
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if self._skip_end_once:
            self._skip_end_once = False
            return
        if self._mode == PlaybackMode.SINGLE_LOOP:
            self._player.setPosition(0)
            self._player.play()
            return
        if self._previewing:
            # The next stream lives behind an LX script, so the search page has
            # to resolve it; the controller only announces the intent.
            if self._preview_index + 1 < len(self._preview_tracks):
                self.previewNextRequested.emit()
            return
        QTimer.singleShot(0, self.next)

    def _on_media_error(self, error: QMediaPlayer.Error, message: str = "") -> None:
        if not message:
            message = str(error)
        self.errorOccurred.emit(f"播放失败：{message}")
