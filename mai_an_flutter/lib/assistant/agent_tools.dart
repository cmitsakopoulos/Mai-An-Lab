/// `llm_tools.get_agent_tools()`: the tool declarations sent to the model,
/// plain JSON-Schema with lowercase types so every OpenAI-compatible
/// endpoint takes them (Gemini's shim, Ollama, LM Studio). Sent exactly as
/// Flet sends them (golden-tested), so the model sees the same tools,
/// including the ones whose features come in later phases (§7).
///
/// Built fresh on each call, as Python's function returns a new list.
List<Map<String, Object?>> agentTools() => [
  {
    "name": "search_library",
    "description":
        "Search the user's LOCAL music library for tracks. Returns matches with their 'path' (the stable identity you pass to other tools). Use this to check what the user owns before playing.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Track title, artist, or album to search for.",
        },
        "genre": {
          "type": "string",
          "description": "Optional genre filter, e.g. 'Jazz'.",
        },
        "limit": {
          "type": "integer",
          "description": "Max results (default 10).",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "play_music",
    "description":
        "Play tracks matching a query immediately, REPLACING the current queue. Resolves the query against the local library (handles artist/title/album). If a non-empty queue would be replaced, the user is asked to confirm first.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "What to play (artist, title, or album).",
        },
        "path": {
          "type": "string",
          "description":
              "Optional exact track path from search_library for a precise pick.",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "enqueue_music",
    "description":
        "Add tracks matching a query to the END of the queue without interrupting playback.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "What to enqueue."},
        "path": {
          "type": "string",
          "description": "Optional exact track path from search_library.",
        },
      },
      "required": ["query"],
    },
  },
  {
    "name": "play_next",
    "description":
        "Insert a track to play right AFTER the current one, without clearing the queue.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "What to play next."},
        "path": {
          "type": "string",
          "description": "Optional exact track path from search_library.",
        },
      },
      "required": ["query"],
    },
  },
  {
    "name": "play_similar",
    "description":
        "Curate more tracks acoustically similar to what is currently playing, using the DSP similarity walk. Use when the user wants 'more like this'.",
    "parameters": {
      "type": "object",
      "properties": <String, Object?>{},
      "required": <Object?>[],
    },
  },
  {
    "name": "steer_mood",
    "description":
        "Build a mood/vibe-based mix from the local library based on any mood descriptor (e.g. 'chill', 'energetic', 'melancholic', 'focus', 'workout', 'dark', 'upbeat', 'rainy day') and start playing it using DSP acoustic feature matching.",
    "parameters": {
      "type": "object",
      "properties": {
        "mood": {
          "type": "string",
          "description": "Target mood or vibe description.",
        },
      },
      "required": ["mood"],
    },
  },
  {
    "name": "search_by_acoustic_profile",
    "description":
        "Search the local music library by acoustic properties extracted by DSP (energy 0.0-1.0, brightness 0.0-1.0, bpm range, genre filters). Use when the user asks for music with specific acoustic traits or energy levels.",
    "parameters": {
      "type": "object",
      "properties": {
        "min_energy": {
          "type": "number",
          "description": "Minimum energy (0.0=calm to 1.0=intense).",
        },
        "max_energy": {
          "type": "number",
          "description": "Maximum energy (0.0 to 1.0).",
        },
        "min_brightness": {
          "type": "number",
          "description":
              "Minimum acoustic brightness (0.0=dark/warm to 1.0=bright).",
        },
        "max_brightness": {
          "type": "number",
          "description": "Maximum acoustic brightness.",
        },
        "min_bpm": {"type": "integer", "description": "Minimum tempo in BPM."},
        "max_bpm": {"type": "integer", "description": "Maximum tempo in BPM."},
        "genre": {
          "type": "string",
          "description": "Optional genre substring filter.",
        },
        "limit": {
          "type": "integer",
          "description": "Max results (default 10).",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "playback_control",
    "description":
        "Control transport: pause, resume, skip, previous, stop, clear_queue, or shuffle.",
    "parameters": {
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "enum": [
            "pause",
            "resume",
            "skip",
            "previous",
            "stop",
            "clear_queue",
            "shuffle",
          ],
          "description": "Transport action to perform.",
        },
      },
      "required": ["action"],
    },
  },
  {
    "name": "save_queue_as_playlist",
    "description":
        "Save the current queue/walk (from the current position onward) as a named playlist.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "Playlist name."},
      },
      "required": ["name"],
    },
  },
  {
    "name": "get_player_status",
    "description":
        "Get the currently playing track, playback state, and queue length.",
    "parameters": {
      "type": "object",
      "properties": <String, Object?>{},
      "required": <Object?>[],
    },
  },
  {
    "name": "get_current_track_info",
    "description":
        "Get detailed metadata (title, artist, album, duration) about the current track.",
    "parameters": {
      "type": "object",
      "properties": <String, Object?>{},
      "required": <Object?>[],
    },
  },
  {
    "name": "search_online",
    "description":
        "Search Qobuz ONLINE for tracks/albums the user may not own locally. Returns results. Set download=true to fetch the top result — this ALWAYS asks the user to confirm first.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "Online search query."},
        "download": {
          "type": "boolean",
          "description":
              "If true, offer to download the top match (requires user confirmation).",
        },
      },
      "required": ["query"],
    },
  },
  {
    "name": "web_search",
    "description":
        "Look anything up on the web that the library can't answer: an artist's story, what a song is about, release dates, who produced or sampled what, band members, new or upcoming releases, tours, what genre or scene something belongs to, translations of titles. Returns a written answer (Google) or search snippets, each with its source. Answer from what it returns, briefly naming the source; never guess facts you could look up.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description":
              "The question or search terms, with the artist name for context.",
        },
      },
      "required": ["query"],
    },
  },
  {
    "name": "learn_about",
    "description":
        "Background on the CURRENT track, its artist or its album (or a named one): what the library knows (album, year, genre, country, plays) plus the web's story. Use for 'tell me about this song', 'who is this', 'what's this album'.",
    "parameters": {
      "type": "object",
      "properties": {
        "subject": {
          "type": "string",
          "enum": ["track", "artist", "album"],
          "description": "What to describe (default: track).",
        },
        "name": {
          "type": "string",
          "description":
              "Optional: a named track/artist/album instead of what's playing.",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "find_related_artists",
    "description":
        "Artists the web calls similar to an artist (default: the one playing), split into those already in the user's library (playable now with play_music / enqueue_music) and the sources naming others (findable with search_online). Use for 'who else sounds like this', 'artists like X', 'build me a mix around X'.",
    "parameters": {
      "type": "object",
      "properties": {
        "artist": {
          "type": "string",
          "description": "Artist name; omit for the current artist.",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "get_library_overview",
    "description":
        "Summary stats of the local library: total tracks, artist/album/playlist counts, and the top genres. Use for 'how big is my library' / 'what genres do I have'.",
    "parameters": {
      "type": "object",
      "properties": <String, Object?>{},
      "required": <Object?>[],
    },
  },
  {
    "name": "get_top_played",
    "description":
        "The user's most-played tracks with play counts. Use for 'my most played', 'favourite songs'.",
    "parameters": {
      "type": "object",
      "properties": {
        "limit": {"type": "integer", "description": "How many (default 10)."},
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "list_artists",
    "description":
        "List artists in the library (with per-artist track & album counts), optionally filtered. Use for 'how many artists', 'do I have <artist>', 'how many tracks by <artist>'.",
    "parameters": {
      "type": "object",
      "properties": {
        "search": {"type": "string", "description": "Optional name filter."},
        "limit": {
          "type": "integer",
          "description": "Max results (default 25).",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "get_artist_albums",
    "description":
        "List the albums the library holds for a given artist (title, year, genre, track count).",
    "parameters": {
      "type": "object",
      "properties": {
        "artist": {"type": "string", "description": "Artist name."},
      },
      "required": ["artist"],
    },
  },
  {
    "name": "get_album_tracks",
    "description":
        "List the tracks on an album, in order. Artist is optional but disambiguates same-named albums.",
    "parameters": {
      "type": "object",
      "properties": {
        "album": {"type": "string", "description": "Album title."},
        "artist": {"type": "string", "description": "Optional artist name."},
      },
      "required": ["album"],
    },
  },
  {
    "name": "list_playlists",
    "description": "List the user's saved playlists with their track counts.",
    "parameters": {
      "type": "object",
      "properties": <String, Object?>{},
      "required": <Object?>[],
    },
  },
  {
    "name": "get_playlist_tracks",
    "description": "List the tracks inside a named playlist, in order.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "Playlist name."},
      },
      "required": ["name"],
    },
  },
  {
    "name": "get_recently_played",
    "description":
        "The most recently played tracks, newest first. Use for 'what did I just play', 'what was I listening to'.",
    "parameters": {
      "type": "object",
      "properties": {
        "limit": {"type": "integer", "description": "How many (default 15)."},
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "get_track_details",
    "description":
        "Rich metadata for one track (album, genre, year, duration, bpm, energy). Pass a query, or a path, or nothing to describe the current track. Use for 'what bpm is this', 'what year is this from'.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {"type": "string", "description": "Track/artist to look up."},
        "path": {
          "type": "string",
          "description": "Exact track path from another tool.",
        },
      },
      "required": <Object?>[],
    },
  },
  {
    "name": "play_album",
    "description":
        "Play a whole album in order, replacing the queue. Artist optional but disambiguates. Confirms first if a queue is already playing.",
    "parameters": {
      "type": "object",
      "properties": {
        "album": {"type": "string", "description": "Album title."},
        "artist": {"type": "string", "description": "Optional artist name."},
      },
      "required": ["album"],
    },
  },
  {
    "name": "play_playlist",
    "description":
        "Play a named playlist in order, replacing the queue. Confirms first if a queue is already playing.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "Playlist name."},
      },
      "required": ["name"],
    },
  },
  {
    "name": "get_metadata_status",
    "description":
        "Metadata health: how many artists Auto-Play can place (have a genre or country), which artists have neither, and AI suggestions waiting for review. Use for 'which artists are missing info', 'is my metadata OK'.",
    "parameters": {
      "type": "object",
      "properties": <String, Object?>{},
      "required": <Object?>[],
    },
  },
  {
    "name": "get_artist_metadata",
    "description":
        "The stored genres and country of one library artist, where each came from, and any AI suggestion on file.",
    "parameters": {
      "type": "object",
      "properties": {
        "artist": {"type": "string", "description": "Artist name."},
      },
      "required": ["artist"],
    },
  },
  {
    "name": "research_artist_metadata",
    "description":
        "Search the web (Google, with sources) for a library artist's genres and country. Saves the answer only as a SUGGESTION for review; changes nothing. Use when the user asks who an artist is, or to fill in a missing genre/country.",
    "parameters": {
      "type": "object",
      "properties": {
        "artist": {"type": "string", "description": "Artist name."},
        "refresh": {
          "type": "boolean",
          "description": "Search again even if an answer is cached.",
        },
      },
      "required": ["artist"],
    },
  },
  {
    "name": "apply_artist_metadata",
    "description":
        "Save genres and/or a country for one or MANY library artists in one call (this changes what Auto-Play does). By default the genres REPLACE what the artists had — use this to correct a wrong genre; mode='add' keeps existing tags. Omit genres and country to accept a single artist's pending AI suggestion. Always confirms with the user first.",
    "parameters": {
      "type": "object",
      "properties": {
        "artist": {
          "type": "string",
          "description": "Artist name (or use 'artists').",
        },
        "artists": {
          "type": "array",
          "items": {"type": "string"},
          "description": "Several artist names to set at once.",
        },
        "genres": {
          "type": "array",
          "items": {"type": "string"},
          "description":
              "Lower-case genre tags. Prefer genres the library already uses.",
        },
        "country": {
          "type": "string",
          "description": "ISO 3166-1 alpha-2 country code.",
        },
        "mode": {
          "type": "string",
          "enum": ["replace", "add"],
          "description": "replace (default) or add to existing genres.",
        },
      },
    },
  },
];
