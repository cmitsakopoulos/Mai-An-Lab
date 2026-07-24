import os
import sys
import logging
import asyncio
import hashlib
import flet as ft

from ui.tokens import BG, SURFACE, SURFACE2, CYAN, TEXT, DIM, BORDER, apply_opacity

if sys.platform == "darwin":
    from utils.audio_engine_macos import audio_engine
else:
    from utils.audio_engine import audio_engine

logger = logging.getLogger(__name__)


class ModelModePill(ft.Container):
    """Sleek radio-pill toggle on the Jarvis header bar allowing the user to
    switch between the AI Agent (LLM tool-calling) and the classic Semantic model."""

    def __init__(self, is_llm: bool = True, on_change=None):
        super().__init__()
        self.is_llm = is_llm
        self.on_change = on_change

        self.border_radius = 18
        self.bgcolor = SURFACE2
        self.border = ft.Border.all(1, BORDER)
        self.padding = ft.Padding.all(3)

        self._llm_icon = ft.Container(
            width=6, height=6, border_radius=3, bgcolor=CYAN, margin=ft.Margin.only(right=4)
        )
        self._llm_text = ft.Text("AI Agent", size=11, weight=ft.FontWeight.W_700)
        self._llm_item = ft.Container(
            content=ft.Row([self._llm_icon, self._llm_text], spacing=0, tight=True),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=14,
            on_click=lambda _: self._toggle(True),
        )

        self._semantic_text = ft.Text("Semantic", size=11, weight=ft.FontWeight.W_700)
        self._semantic_item = ft.Container(
            content=self._semantic_text,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border_radius=14,
            on_click=lambda _: self._toggle(False),
        )

        self.content = ft.Row([self._llm_item, self._semantic_item], spacing=2, tight=True)
        self._apply_styles()

    def _apply_styles(self):
        if self.is_llm:
            self._llm_item.bgcolor = CYAN
            self._llm_text.color = BG
            self._llm_icon.bgcolor = BG
            self._semantic_item.bgcolor = ft.Colors.TRANSPARENT
            self._semantic_text.color = DIM
        else:
            self._semantic_item.bgcolor = CYAN
            self._semantic_text.color = BG
            self._llm_item.bgcolor = ft.Colors.TRANSPARENT
            self._llm_text.color = DIM
            self._llm_icon.bgcolor = DIM

    def _toggle(self, enable_llm: bool):
        if self.is_llm != enable_llm:
            self.is_llm = enable_llm
            self._apply_styles()
            if self.page:
                self.update()
            if self.on_change:
                self.on_change(self.is_llm)


class AssistantView:
    """Integrated chat surface for the faux-AI assistant.

    Owns the chat scrollback, the input field, and the initialisation banner
    (which surfaces the DSP analyser sweep + graph-build progress).
    """

    def __init__(self, app: "StreamripFletApp"):
        self.app = app
        self.page = app.page
        self._initialized = False
        self.layout: ft.Column | None = None
        # Runner is created lazily so it picks up the DB + engine after they
        # have themselves finished setting up.
        self._runner = None
        # Concurrency guard: only one init pass at a time. There is NO
        # _init_done flag — init is intentionally re-run on every open so
        # newly-added tracks get surfaced as a confirmation prompt without
        # needing a manual reset.
        self._init_started = False
        # Suppresses the "Hi, ready" greeting on subsequent opens within
        # the same session. Reset to False when LibraryView finishes a scan
        # (so the next open re-greets, surfacing newly-scanned tracks).
        self._init_greeted = False
        self._tts_enabled = True
        # Cancellation hook for the analyser sweep so the user can dismiss
        # the panel without leaving a background analyser running forever.
        self._init_cancel = False
        self._analysing_library = False

        from utils.chat_memory import ChatMemoryManager
        self.chat_memory = ChatMemoryManager()
        self._history_list = []

    def _on_mode_toggle(self, is_llm: bool):
        from utils.streamrip_api import update_config_params
        update_config_params({
            "assistant": {
                "llm_enabled": is_llm
            }
        })
        label = "AI Agent" if is_llm else "Semantic Parser"
        self.app.show_snackbar(f"Jarvis engine mode set to: {label}")

    def _ensure_initialized(self):
        if self._initialized:
            return

        self._messages = ft.ListView(
            expand=True,
            spacing=8,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            auto_scroll=False,
            # Android shows no scrollbar unless `scroll` is set (mobile
            # ScrollBehavior adds none); the page ScrollbarTheme styles it.
            scroll=ft.ScrollMode.ALWAYS,
        )

        self._input = ft.TextField(
            hint_text="Ask me anything, sir…",
            border_color=BORDER,
            focused_border_color=CYAN,
            text_style=ft.TextStyle(color=TEXT, size=14),
            content_padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            border_radius=22,
            multiline=False,
            min_lines=1,
            max_lines=1,
            expand=True,
            on_submit=lambda _e: self._on_send_click(),
        )

        self._send_btn = ft.IconButton(
            icon=ft.Icons.ARROW_UPWARD_ROUNDED,
            icon_color=CYAN,
            on_click=lambda _e: self._on_send_click(),
        )

        # Push-to-talk mic. Pressing and holding starts a listening session via
        # speech_to_text; releasing stops it. While listening, the icon
        # turns red so the user can see we're capturing audio. STT runs
        # through the audio_service bridge.
        self._mic_icon = ft.Icon(ft.Icons.MIC_ROUNDED, color=CYAN)
        self._mic_btn = ft.GestureDetector(
            content=ft.Container(
                content=self._mic_icon,
                padding=10,
                border_radius=20,
            ),
            tooltip="Hold to Speak",
            # Touch-down starts listening immediately. Release stops it.
            #   • short tap   → on_tap_up
            #   • held button → on_long_press_end (Flutter cancels the tap
            #     gesture once long-press wins arbitration, so on_tap_cancel
            #     would fire mid-hold — we deliberately don't wire it).
            on_tap_down=self._on_mic_down,
            on_tap_up=self._on_mic_up,
            on_long_press_end=self._on_mic_up,
        )
        self._stt_listening = False
        self._mic_pressed = False

        self._tts_toggle = ft.IconButton(
            icon=ft.Icons.VOLUME_UP_ROUNDED,
            icon_color=CYAN,
            tooltip="Toggle voice replies",
            on_click=lambda _e: self._toggle_tts(),
        )

        self._clear_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
            icon_color=CYAN,
            tooltip="Clear conversation history",
            on_click=lambda _e: self.clear_chat_manually(),
        )

        # Radio pill mode toggle (AI Agent vs Semantic model)
        is_llm_active = True
        try:
            from utils.streamrip_api import load_config
            cfg = load_config()
            is_llm_active = bool(cfg.get("assistant", {}).get("llm_enabled", True))
        except Exception:
            pass

        self._mode_pill = ModelModePill(
            is_llm=is_llm_active,
            on_change=self._on_mode_toggle,
        )

        # Load session history
        session = self.chat_memory.load_session()
        self._init_greeted = session["init_greeted"]
        self._history_list = session["messages"]

        restored_controls = []
        for msg in self._history_list:
            restored_controls.append(self._build_bubble_row(msg))

        if restored_controls:
            self._messages.controls = restored_controls
        else:
            self._messages.controls = [self._build_empty_state()]

        self.layout = ft.Column(
            [
                # Header
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Text("JARVIS", color=TEXT, size=13,
                                    weight=ft.FontWeight.W_700),
                            ft.Container(expand=True),
                            self._mode_pill,
                            ft.Container(width=4),
                            self._clear_btn,
                            self._tts_toggle,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                ),
                ft.Divider(color=BORDER, height=1),
                # Messages Slot
                ft.Container(content=self._messages, expand=True),
                ft.Divider(color=BORDER, height=1),
                # Footer Input
                ft.Container(
                    content=ft.Row(
                        [self._mic_btn, self._input, self._send_btn],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.only(
                        left=12, right=8, top=8, bottom=24,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )
        self._initialized = True

    def build(self) -> ft.Control:
        self._ensure_initialized()
        return self.layout

    # ── Public lifecycle ───────────────────────────────────────────────────

    def expand(self):
        # In Tab-mode, expand is simply a trigger for lazy initialization.
        self.page.run_task(self._init_assistant)

    def collapse(self):
        # No-op in Tab-mode; visibility is handled by the tab-switcher.
        pass
        # Stop any in-flight TTS so the user isn't talked at after dismissing.
        service = getattr(audio_engine, "audio_service", None)
        if service is not None:
            self.page.run_task(service.tts_stop)

    # ── Initialisation flow ────────────────────────────────────────────────

    async def _init_assistant(self):
        """Inspect graph state and decide what to surface to the user.

        Cheap operations (building metadata edges from existing data,
        rebuilding the acoustic graph when features are already present)
        run silently in the background — they take seconds and don't need
        confirmation. The expensive DSP analyser sweep is *always* offered
        as a confirmation prompt, never auto-run, because it can take hours
        on large libraries and consume battery the user didn't ask for.

        Re-runs are cheap and safe: every open just re-reads status and
        either says "Ready" or asks again about pending analysis work.
        _init_done is intentionally absent — making this stateless means
        new tracks added between opens automatically get surfaced as a new
        prompt without needing a manual reset."""
        if self._init_started:
            return
        self._init_started = True
        try:
            await self._do_init()
        finally:
            self._init_started = False

    async def _do_init(self):
        await asyncio.sleep(0.2)
        
        if hasattr(self, "_messages") and self._messages:
            try:
                await self._messages.scroll_to(offset=-1, duration=0)
            except Exception:
                pass

        logger.info("AssistantView: init flow started")

        from utils.assistant_runner import AssistantRunner
        from utils import track_graph as tg
        if self._runner is None:
            self._runner = AssistantRunner(self.app.db_manager, audio_engine)

        if audio_engine.audio_service:
            self.page.run_task(
                audio_engine.audio_service.tts_set_voice, pitch=0.75, rate=0.75
            )

        try:
            status = await tg.graph_status(self.app.db_manager)
        except Exception as exc:
            logger.exception("AssistantView: graph_status failed")
            await self._append_bubble(
                "assistant",
                f"Couldn't read your library: {exc}",
            )
            return

        if status["total_tracks"] == 0:
            if not self._init_greeted:
                self._init_greeted = True
                self.chat_memory.save_session(self._history_list, self._init_greeted)
                has_empty_msg = any(
                    msg["sender"] == "assistant" and "Your library looks empty" in msg["text"]
                    for msg in self._history_list
                )
                if not has_empty_msg:
                    await self._append_bubble(
                        "assistant",
                        "Your library looks empty. Scan a music folder in Library → Scan to get started.",
                    )
            return

        graph_state = self.chat_memory.load_graph_state()
        needs_metadata = (status["artist_edges"] == 0 and status["album_edges"] == 0)
        needs_acoustic = (status["coord_tracks"] == 0 and status["total_tracks"] >= 2)
        is_up_to_date = (
            graph_state.get("total_tracks") == status["total_tracks"]
            and not needs_acoustic
            and not needs_metadata
        )

        if not is_up_to_date:
            try:
                await tg.build_metadata_edges(self.app.db_manager)
                await tg.build_acoustic_edges(self.app.db_manager)
                self.chat_memory.save_graph_state(status["total_tracks"], 0)
            except Exception as exc:
                logger.warning("AssistantView: edge build failed: %s", exc)

        if not self._init_greeted:
            self._init_greeted = True
            self.chat_memory.save_session(self._history_list, self._init_greeted)

    # ── Chat plumbing ──────────────────────────────────────────────────────

    def _toggle_tts(self):
        self._tts_enabled = not self._tts_enabled
        self._tts_toggle.icon = (
            ft.Icons.VOLUME_UP_ROUNDED if self._tts_enabled
            else ft.Icons.VOLUME_OFF_ROUNDED
        )
        if not self._tts_enabled:
            service = getattr(audio_engine, "audio_service", None)
            if service is not None:
                self.page.run_task(service.tts_stop)
        self.app.safe_update(lambda: None)

    def _on_send_click(self):
        text = (self._input.value or "").strip()
        if not text:
            return
        self._input.value = ""
        self.app.safe_update(lambda: None)
        self.page.run_task(self._handle_user_text, text)

    def _on_mic_down(self, e):
        self._mic_pressed = True
        self.page.run_task(self._start_listening)

    def _on_mic_up(self, e):
        self._mic_pressed = False
        self.page.run_task(self._stop_listening)

    async def _start_listening(self):
        if getattr(self, "_analysing_library", False):
            self.app.show_snackbar("Please wait until library analysis is complete.")
            self._mic_pressed = False
            return

        if self._stt_listening:
            return

        service = getattr(audio_engine, "audio_service", None)
        is_mock = (service is None)

        # First tap: ensure mic permission (skip if mock mode)
        if not is_mock:
            try:
                perms = await service.query_permissions()
                if perms.get("record_audio") != "granted":
                    res = await service.request_permission("record_audio")
                    if res.get("status") != "granted":
                        self.app.show_snackbar(
                            "Microphone permission is required for voice commands."
                        )
                        self._mic_pressed = False
                        return
            except Exception as ex:
                logger.warning(f"Mic permission check failed: {ex}")
                self.app.show_snackbar("Couldn't verify microphone permission.")
                self._mic_pressed = False
                return

        # Safeguard check: If the user released the button while we were awaiting permissions, abort!
        if not self._mic_pressed:
            logger.info("AssistantView: user released mic before initialization completed. Aborting STT start.")
            return

        self._stt_listening = True
        self._mic_icon.color = "#FF4444"
        self._mic_btn.tooltip = "[MOCK MODE] Release to Send" if is_mock else "Release to Send"
        self._mic_icon.update()
        self._mic_btn.update()

        self._listening_icon = ft.Icon(ft.Icons.MIC_ROUNDED, color="#FF4444", size=18)
        self._listening_text = ft.Text(
            "Listening (mock mode), sir..." if is_mock else "Listening, sir...",
            color=DIM, size=13, italic=True
        )
        self._listening_bubble = ft.Row(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            self._listening_icon,
                            self._listening_text,
                            ft.ProgressRing(width=12, height=12, stroke_width=1.5, color=CYAN),
                        ],
                        spacing=8,
                        alignment=ft.MainAxisAlignment.START,
                    ),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    bgcolor=SURFACE2,
                    border_radius=14,
                    border=ft.Border.all(1, apply_opacity(0.1, CYAN)),
                )
            ],
            alignment=ft.MainAxisAlignment.START,
        )

        def _show_listening():
            # Drop the empty-state on first real message.
            if self._messages.controls and isinstance(self._messages.controls[0], ft.Container) \
                    and not isinstance(getattr(self._messages.controls[0], "content", None), ft.Row):
                if len(self._messages.controls) == 1:
                    self._messages.controls = []
            self._messages.controls.append(self._listening_bubble)
            self._messages.update()
        self.app.safe_update(_show_listening)
        
        # Smoothly scroll to the bottom after the listening bubble is added
        await asyncio.sleep(0.06)
        try:
            await self._messages.scroll_to(offset=-1, duration=200)
        except Exception:
            pass

        try:
            if not is_mock:
                # 60 s upper bound on a single hold (the plugin still
                # finalises early when stt_stop() is called on release).
                stt_task = asyncio.create_task(service.stt_listen(timeout=60.0))
                
                released = False
                release_time = None
                while not stt_task.done():
                    if not self._stt_listening and not released:
                        released = True
                        release_time = asyncio.get_running_loop().time()
                    
                    if released:
                        elapsed = asyncio.get_running_loop().time() - release_time
                        if elapsed > 4.0:
                            logger.info("AssistantView: Post-release transcription timeout. Cancelling STT task.")
                            stt_task.cancel()
                            break
                    await asyncio.sleep(0.05)
                
                if stt_task.done() and not stt_task.cancelled():
                    try:
                        res = stt_task.result()
                    except Exception as e:
                        logger.warning(f"stt_task failed with exception: {e}")
                        res = {"ok": False, "error": str(e)}
                else:
                    res = {"ok": False, "error": "cancelled"}
            else:
                # Simulated Speech-to-Text session for offline debugging on macOS
                # Check if self._stt_listening is flipped to False every 0.05 seconds
                timeout_counter = 0.0
                while self._stt_listening and timeout_counter < 15.0:
                    await asyncio.sleep(0.05)
                    timeout_counter += 0.05
                typed = (self._input.value or "").strip()
                res = {"ok": True, "text": typed if typed else "play random"}

            if res.get("ok") and res.get("text"):
                self._input.value = res["text"]
                self.app.safe_update(lambda: None)
                self._on_send_click()
            elif res.get("error") and all(x not in res["error"].lower() for x in ["cancel", "no_match", "no match"]):
                self.app.show_snackbar(f"Speech error: {res['error']}")
        except asyncio.TimeoutError:
            # No utterance recognised in the listen window; silent no-op.
            pass
        except Exception as ex:
            logger.error(f"STT Error: {ex}")
        finally:
            self._stt_listening = False
            self._mic_icon.color = CYAN
            self._mic_btn.tooltip = "Hold to Speak"
            
            def _hide_listening():
                if hasattr(self, "_listening_bubble") and self._listening_bubble in self._messages.controls:
                    self._messages.controls.remove(self._listening_bubble)
                    self._messages.update()
                self._mic_icon.update()
                self._mic_btn.update()
            self.app.safe_update(_hide_listening)

    async def _stop_listening(self):
        if not self._stt_listening:
            return
        
        self._stt_listening = False
        
        # Instantly update UI on finger lift so microphone looks closed
        # and Jarvis shows "Thinking..." state during the transcription delay.
        def _show_processing():
            self._mic_icon.color = CYAN
            self._mic_btn.tooltip = "Hold to Speak"
            self._mic_icon.update()
            self._mic_btn.update()
            
            if hasattr(self, "_listening_icon") and hasattr(self, "_listening_text"):
                self._listening_icon.name = ft.Icons.AUTO_AWESOME_ROUNDED
                self._listening_icon.color = CYAN
                self._listening_text.value = "Thinking..."
                self._listening_icon.update()
                self._listening_text.update()
        
        self.app.safe_update(_show_processing)
        
        service = getattr(audio_engine, "audio_service", None)
        if service is not None:
            try:
                await service.stt_stop()
            except Exception as ex:
                logger.warning(f"stt_stop failed: {ex}")

    async def _handle_user_text(self, text: str):
        await self._append_bubble("user", text)
        if getattr(self, "_analysing_library", False):
            await self._append_bubble(
                "assistant",
                "Please stand by, sir. I am currently busy analyzing the library.",
            )
            return

        if self._runner is None:
            await self._append_bubble(
                "assistant",
                "Please stand by, sir. The system is still initializing.",
            )
            return

        # Mirror the voice flow: show a "Thinking..." bubble while the runner
        # works so text-only users get the same feedback as voice users.
        thinking_bubble = ft.Row(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=CYAN, size=18),
                            ft.Text("Thinking...", color=DIM, size=13, italic=True),
                            ft.ProgressRing(width=12, height=12, stroke_width=1.5, color=CYAN),
                        ],
                        spacing=8,
                        alignment=ft.MainAxisAlignment.START,
                    ),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    bgcolor=SURFACE2,
                    border_radius=14,
                    border=ft.Border.all(1, apply_opacity(0.1, CYAN)),
                )
            ],
            alignment=ft.MainAxisAlignment.START,
        )
        def _show_thinking():
            self._messages.controls.append(thinking_bubble)
            self._messages.update()
        self.app.safe_update(_show_thinking)

        # Hand the runner a live snapshot of the chat history so it can
        # resolve anaphora without round-tripping through chat_history.json
        # on every utterance.
        try:
            response = await self._runner.dispatch_text(
                text, history_provider=lambda: list(self._history_list),
            )
        finally:
            def _hide_thinking():
                if thinking_bubble in self._messages.controls:
                    self._messages.controls.remove(thinking_bubble)
                    self._messages.update()
            self.app.safe_update(_hide_thinking)
        # Build a structured entity dict from the response so future turns
        # can resolve pronouns without any regex or DB round-trip.
        # Non-canonical seeds (play_random, play_similar_bulk) carry
        # an entity_intent marker — their "track"/"first" is a system-picked
        # seed, NOT the user's referent, so we don't anchor pronouns on it.
        # Multi-match plays (extras.is_multi) are similar: the opener was
        # randomly picked from many hits, but the artist (if consistent) is
        # still meaningful for "more by them" follow-ups.
        _NON_CANONICAL_SEEDS = {"play_random", "play_similar_bulk"}
        _entity_intent = response.extras.get("entity_intent")
        _is_non_canonical = _entity_intent in _NON_CANONICAL_SEEDS
        _is_multi = bool(response.extras.get("is_multi"))
        _raw_track = response.extras.get("track") or response.extras.get("first")
        if _is_non_canonical:
            _track = None
            _artist = None
        elif _is_multi:
            _track = None        # random opener is not the anchor
            _artist = (_raw_track.get("artist") or _raw_track.get("artist_name")
                       if _raw_track else None)
        else:
            _track = _raw_track
            _artist = (_track.get("artist") or _track.get("artist_name")
                       if _track else response.extras.get("artist"))
        intent = getattr(response, "intent", None)
        # Thread AI-agent provenance onto the intent so the bubble can render the
        # Stage 3 badge — response.extras is not otherwise persisted on the msg.
        agent_prov = response.extras.get("agent")
        if agent_prov and intent is not None:
            try:
                intent.extras["agent"] = agent_prov
            except Exception:
                pass
        _entities = {
            "track":  _track,
            "artist": _artist,
            "playlist": response.extras.get("playlist"),
            "intent": intent.name if intent else getattr(response, "_intent_name", None),
        }

        await self._append_bubble(
            "assistant", response.displayed,
            speak=response.success and bool(response.spoken),
            speak_text=response.spoken,
            entities=_entities if any(_entities.values()) else None,
            intent=intent,
        )
        # Playback intents stage the queue but leave engine.play() to us so
        # Jarvis finishes his sentence before the music starts. _append_bubble
        # awaits the TTS future before returning, so by here it's safe to
        # kick off playback. Guarded so a failed-intent response that still
        # has deferred_play set (shouldn't happen, but defensive) doesn't
        # start audio on top of an error message.
        if response.success and response.deferred_play:
            try:
                audio_engine.play()
            except Exception as exc:
                logger.warning("AssistantView: deferred play failed: %s", exc)

        # Update shuffle button color in Now Playing if state changed in audio_engine.
        # Without page.update() the icon_color assignment doesn't repaint until the
        # next unrelated UI event, so play_random's shuffle activation looked silent.
        try:
            if hasattr(self.app, "now_playing") and self.app.now_playing:
                self.app.now_playing.update_shuffle(audio_engine.is_shuffle)
                self.app.page.update()
        except Exception:
            pass

    async def _append_bubble(
        self,
        sender: str,
        text: str,
        speak: bool = False,
        speak_text: str | None = None,
        entities: dict | None = None,
        intent = None,
    ):
        # Update in-memory history list. Persist structured entity data on
        # assistant messages so _resolve_anaphora can do a plain dict lookup
        # instead of regex-parsing markdown bold tags.
        msg: dict = {"sender": sender, "text": text}
        if entities:
            msg["entities"] = entities
        if intent:
            msg["intent"] = {
                "name": intent.name if hasattr(intent, "name") else intent.get("name"),
                "query": intent.query if hasattr(intent, "query") else intent.get("query"),
                "raw": intent.raw if hasattr(intent, "raw") else intent.get("raw"),
                "extras": intent.extras if hasattr(intent, "extras") else intent.get("extras", {}),
            }

        row = self._build_bubble_row(msg)
        self._history_list.append(msg)
        if len(self._history_list) > 50:
            self._history_list.pop(0)

        # Save to disk
        self.chat_memory.save_session(self._history_list, self._init_greeted)

        def _mutate():
            # Drop the empty-state on first real message.
            if self._messages.controls and isinstance(self._messages.controls[0], ft.Container) \
                    and not isinstance(getattr(self._messages.controls[0], "content", None), ft.Row):
                if len(self._messages.controls) == 1:
                    self._messages.controls = []
            self._messages.controls.append(row)
            
            # Proactive performance cap: keep history limited to 50 bubbles maximum
            if len(self._messages.controls) > 50:
                self._messages.controls.pop(0)
        self.app.safe_update(_mutate)

        # Smoothly scroll to the bottom after the UI has flushed and rendered.
        # Tall bubbles finish painting after the first scroll fires, leaving the
        # new content below the fold — so we do a second pass once layout has settled.
        await asyncio.sleep(0.06)
        try:
            await self._messages.scroll_to(offset=-1, duration=200)
        except Exception:
            pass
        await asyncio.sleep(0.25)
        try:
            await self._messages.scroll_to(offset=-1, duration=120)
        except Exception:
            pass

        if speak and self._tts_enabled:
            service = getattr(audio_engine, "audio_service", None)
            if service is not None:
                try:
                    # Pause music briefly so TTS isn't drowned out. Resume
                    # afterwards only if we were the ones who paused.
                    was_playing = bool(getattr(audio_engine, "is_playing", False))
                    if was_playing:
                        audio_engine.pause()
                    await service.tts_speak(speak_text or text, timeout=30.0)
                    if was_playing:
                        audio_engine.play()
                except Exception as exc:
                    logger.warning("AssistantView: TTS speak failed: %s", exc)

    def _build_bubble_row(self, msg: dict) -> ft.Row:
        sender = msg.get("sender", "assistant")
        text = msg.get("text", "")
        is_user = (sender == "user")

        if is_user:
            bubble_content = ft.Text(
                text,
                color="#FFFFFF",
                size=13,
                selectable=True,
            )
        else:
            bubble_content = ft.Markdown(
                value=text,
                selectable=True,
                extension_set=ft.MarkdownExtensionSet.GITHUB_FLAVORED,
                code_theme="github-dark",
                auto_follow_links=True,
            )

        intent = msg.get("intent")
        if not is_user and intent:
            intent_name = intent.get("name", "unknown")
            extras = intent.get("extras") or {}
            is_semantic = extras.get("semantic", False)

            # Stage 1: Regex
            if intent_name != "unknown" and not is_semantic:
                s1_text = f"Stage 1: Regex (Matched: {intent_name})"
                s1_icon = ft.Icons.CHECK_CIRCLE_OUTLINE
                s1_color = "#81C784"
                
                s2_text = "Stage 2: VLM (Skipped)"
                s2_icon = ft.Icons.REMOVE_CIRCLE_OUTLINE
                s2_color = DIM
            elif is_semantic:
                s1_text = "Stage 1: Regex (No Match)"
                s1_icon = ft.Icons.CANCEL_OUTLINED
                s1_color = "#E57373"
                
                score = extras.get("score")
                score_str = f" @ {score:.2f}" if score is not None else ""
                duration = extras.get("compute_time_windows_ms")
                dur_str = f" in {duration:.1f}ms" if duration is not None else ""
                s2_text = f"Stage 2: VLM (Matched: {intent_name}{score_str}{dur_str})"
                s2_icon = ft.Icons.CHECK_CIRCLE_OUTLINE
                s2_color = "#81C784"
            else:
                s1_text = "Stage 1: Regex (No Match)"
                s1_icon = ft.Icons.CANCEL_OUTLINED
                s1_color = "#E57373"

                s2_text = "Stage 2: VLM (No Match)"
                s2_icon = ft.Icons.CANCEL_OUTLINED
                s2_color = "#E57373"

            # Stage 3: AI Agent. When the LLM tool-calling agent handled the turn
            # it supersedes the semantic stage (which is bypassed in agent mode).
            agent = extras.get("agent") if isinstance(extras, dict) else None
            s3 = None
            if agent and agent.get("used_llm"):
                s2_text = "Stage 2: VLM (Skipped)"
                s2_icon = ft.Icons.REMOVE_CIRCLE_OUTLINE
                s2_color = DIM
                seen_tools = []
                for t in (agent.get("tools") or []):
                    if t not in seen_tools:
                        seen_tools.append(t)
                tool_str = (" · " + ", ".join(seen_tools)) if seen_tools else ""
                s3 = (ft.Icons.AUTO_AWESOME_ROUNDED, f"Stage 3: AI Agent (Handled{tool_str})", "#81C784")

            column_children = [
                bubble_content,
                ft.Container(height=1, bgcolor="#262626", margin=ft.Margin.symmetric(vertical=6)),
            ]

            options = extras.get("options")
            if options and not is_user:
                option_controls = []
                for opt in options:
                    opt_id = str(opt.get("id", ""))
                    opt_title = str(opt.get("title", ""))
                    opt_sub = str(opt.get("subtitle", ""))
                    btn = ft.OutlinedButton(
                        f"{opt_id}. {opt_title}",
                        tooltip=opt_sub if opt_sub else None,
                        style=ft.ButtonStyle(
                            color=CYAN,
                            side=ft.BorderSide(1, CYAN),
                        ),
                        on_click=lambda _e, tid=opt_id: self.page.run_task(self._handle_user_text, tid),
                    )
                    option_controls.append(btn)
                column_children.append(ft.Column(option_controls, spacing=4))
                column_children.append(ft.Container(height=1, bgcolor="#262626", margin=ft.Margin.symmetric(vertical=6)))

            stage_specs = [
                (s1_icon, s1_text, s1_color),
                (s2_icon, s2_text, s2_color),
            ]
            if s3 is not None:
                stage_specs.append(s3)
            column_children.extend([
                ft.Row(
                    [
                        ft.Icon(ic, color=col, size=11),
                        ft.Text(tx, color=col, size=10, weight=ft.FontWeight.W_500),
                    ],
                    spacing=6,
                    alignment=ft.MainAxisAlignment.START,
                )
                for (ic, tx, col) in stage_specs
            ])

            bubble_content = ft.Column(
                column_children,
                spacing=4,
                tight=True,
            )

        bubble = ft.Container(
            content=bubble_content,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            bgcolor=CYAN if is_user else SURFACE2,
            border_radius=14,
            width=290 if (len(text) > 25 or intent) else None,
        )
        return ft.Row(
            [bubble],
            alignment=(
                ft.MainAxisAlignment.END if is_user
                else ft.MainAxisAlignment.START
            ),
        )

    def _build_empty_state(self) -> ft.Container:
        prompts = [
            ("[>] Play something chill", "play something chill"),
            ("[*] Play similar tracks", "play similar"),
            ("[#] Library stats", "how big is my library?"),
            ("[?] What can you do?", "help"),
        ]

        chips = [
            ft.Container(
                content=ft.Text(label, color=CYAN, size=12, weight=ft.FontWeight.W_600),
                padding=ft.Padding.symmetric(horizontal=14, vertical=8),
                border_radius=20,
                bgcolor=SURFACE2,
                border=ft.Border.all(1, apply_opacity(0.3, CYAN)),
                ink=True,
                on_click=lambda _e, q=query: self.page.run_task(self._handle_user_text, q),
            )
            for label, query in prompts
        ]

        return ft.Container(
            content=ft.Column(
                [
                    ft.Container(height=16),
                    ft.Icon(ft.Icons.AUTO_AWESOME_ROUNDED, color=CYAN, size=38),
                    ft.Text(
                        "JARVIS",
                        color=TEXT,
                        size=18,
                        weight=ft.FontWeight.W_900,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "At your service, sir. Select a suggestion or speak a directive:",
                        color=DIM,
                        size=12,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Container(height=6),
                    ft.Row(
                        chips,
                        wrap=True,
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=8,
                        run_spacing=8,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            padding=ft.Padding.symmetric(horizontal=20, vertical=20),
            alignment=ft.Alignment(0, 0),
        )

    def clear_chat_manually(self):
        """Wipes the session history on disk and clears it in the UI."""
        self._history_list = []
        self._init_greeted = False
        self.chat_memory.clear_session()
        
        def _mutate():
            self._messages.controls = [self._build_empty_state()]
        self.app.safe_update(_mutate)

    def handle_app_background(self):
        """Invoked when the app is backgrounded to write the latest timestamp."""
        self.chat_memory.touch_session()

    def handle_app_resume(self):
        """Invoked when returning to foreground. If expired, clears UI and re-triggers greet."""
        session = self.chat_memory.load_session()
        # If the session was expired, load_session cleared it, returning empty lists
        if not session["messages"] and self._history_list:
            logger.info("Chat Memory: Inactivity timeout reached. Resetting conversation.")
            self._history_list = []
            self._init_greeted = False
            
            def _mutate():
                self._messages.controls = [self._build_empty_state()]
            self.app.safe_update(_mutate)
            
            # Re-trigger initialisation/greeting flow
            self.page.run_task(self._init_assistant)
