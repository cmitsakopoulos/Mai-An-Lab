import os
import json
import time
import logging

logger = logging.getLogger("streamrip.chat_memory")

class ChatMemoryManager:
    def __init__(self, filename="chat_history.json", timeout_seconds=900):
        self.filename = filename
        self.timeout_seconds = timeout_seconds

    def _get_history_path(self) -> str:
        """Resolves the persistent sandboxed storage path for Android, with fallback for desktop."""
        import sys
        IS_ANDROID = hasattr(sys, 'getandroidapilevel')
        if IS_ANDROID:
            base_dir = os.getenv("FLET_APP_STORAGE_DATA") or os.getenv("APP_FILES_PATH") or "/data/user/0"
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, self.filename)

    def load_session(self) -> dict:
        """Loads and returns the active chat session from disk.
        If the session has expired, it clears it and returns a clean session.
        """
        path = self._get_history_path()
        if not os.path.exists(path):
            return self._empty_session()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to load chat history: %s", e)
            return self._empty_session()

        # Check for lazy expiration
        last_active = data.get("last_active_timestamp", 0.0)
        now = time.time()
        if now - last_active > self.timeout_seconds:
            logger.info("Chat session expired lazily after inactivity threshold.")
            self.clear_session()
            return self._empty_session()

        return data

    def save_session(self, messages: list, init_greeted: bool) -> None:
        """Persists the session to disk with the current timestamp and a cap of 50 messages."""
        path = self._get_history_path()
        data = {
            "messages": messages[-50:],  # Proactive performance cap
            "init_greeted": init_greeted,
            "last_active_timestamp": time.time()
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error("Failed to save chat session: %s", e)

    def touch_session(self) -> None:
        """Updates the active timestamp to the current time, ensuring the session stays alive."""
        session = self.load_session()
        # If there are no messages, no need to touch or keep an empty session file
        if not session["messages"]:
            return
        self.save_session(session["messages"], session["init_greeted"])

    def clear_session(self) -> None:
        """Wipes the stored session file entirely."""
        path = self._get_history_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning("Failed to delete chat history file: %s", e)

    def _empty_session(self) -> dict:
        return {
            "messages": [],
            "init_greeted": False,
            "last_active_timestamp": 0.0
        }
