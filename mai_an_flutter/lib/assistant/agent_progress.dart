import '../core/py_text.dart';

/// Live progress of one agent turn, so a multi-step LLM run shows what it is
/// doing instead of a bare "Thinking..." pill.

/// One thing that happened inside [AssistantRunner.dispatchLlm].
sealed class AgentEvent {
  const AgentEvent();
}

/// A model round trip began (0-based [step]).
class AgentStepStarted extends AgentEvent {
  const AgentStepStarted(this.step);
  final int step;
}

/// A tool call is about to run.
class AgentToolStarted extends AgentEvent {
  const AgentToolStarted(this.id, this.name, this.args);
  final String id;
  final String name;
  final Map<String, Object?> args;
}

/// A tool call returned; [summary] is a short human note ('' when none).
class AgentToolFinished extends AgentEvent {
  const AgentToolFinished(
    this.id,
    this.name, {
    required this.ok,
    this.summary = '',
  });
  final String id;
  final String name;
  final bool ok;
  final String summary;
}

/// Reply text streamed from the model in the current step.
class AgentReplyDelta extends AgentEvent {
  const AgentReplyDelta(this.text);
  final String text;
}

typedef AgentEventSink = void Function(AgentEvent event);

enum ToolStepState { running, done, failed }

/// One row of the live checklist (and of the saved trail).
class ToolStep {
  ToolStep({required this.id, required this.name, required this.label});

  final String id;
  final String name;
  final String label;
  ToolStepState state = ToolStepState.running;
  String summary = '';

  /// The persisted form, kept on the final message as `msg['tools']`.
  Map<String, Object?> toJson() => {
    'name': name,
    'label': label,
    'ok': state != ToolStepState.failed,
    if (summary.isNotEmpty) 'summary': summary,
  };
}

/// The in-flight turn the chat renders while the agent works.
class LiveTurn {
  final List<ToolStep> steps = [];
  int modelSteps = 0;

  /// Reply text streamed so far in the current model step.
  String draft = '';

  /// The latest text the model sent alongside tool calls ("Let me check
  /// your library…"), shown above the checklist but not kept.
  String interim = '';

  /// True when no tool is running and no reply text has arrived yet: the
  /// model is working on its next step.
  bool get awaitingModel =>
      draft.isEmpty && steps.every((s) => s.state != ToolStepState.running);

  void apply(AgentEvent e) {
    switch (e) {
      case AgentStepStarted(:final step):
        modelSteps = step + 1;
        draft = '';
      case AgentReplyDelta(:final text):
        draft += text;
      case AgentToolStarted(:final id, :final name, :final args):
        if (draft.trim().isNotEmpty) interim = draft.trim();
        draft = '';
        steps.add(ToolStep(id: id, name: name, label: toolLabel(name, args)));
      case AgentToolFinished(:final id, :final ok, :final summary):
        for (final s in steps.reversed) {
          if (s.id == id && s.state == ToolStepState.running) {
            s.state = ok ? ToolStepState.done : ToolStepState.failed;
            s.summary = summary;
            break;
          }
        }
    }
  }
}

String _arg(Map<String, Object?> args, String key) {
  final v = args[key];
  return v is String ? pyStrip(v) : '';
}

String _q(String s) {
  final r = s.runes;
  final cut = r.length > 40 ? '${String.fromCharCodes(r.take(39))}…' : s;
  return '“$cut”';
}

/// A short present-tense label for [name] called with [args].
String toolLabel(String name, Map<String, Object?> args) {
  final query = _arg(args, 'query');
  final artist = _arg(args, 'artist');
  final nameArg = _arg(args, 'name');
  final album = _arg(args, 'album');
  // 'Searching the web for' + '' reads as 'Searching the web'.
  String withQ(String base, String v) {
    if (v.isNotEmpty) return '$base ${_q(v)}';
    for (final tail in const [' for', ' toward', ' like', ' by', ':']) {
      if (base.endsWith(tail)) {
        return base.substring(0, base.length - tail.length);
      }
    }
    return base;
  }

  return switch (name) {
    'search_library' =>
      query.isEmpty
          ? 'Searching your library'
          : withQ('Searching your library for', query),
    'play_music' =>
      query.isEmpty ? 'Starting playback' : withQ('Playing', query),
    'enqueue_music' =>
      query.isEmpty ? 'Adding to the queue' : withQ('Queueing', query),
    'play_next' =>
      query.isEmpty ? 'Setting what plays next' : withQ('Playing next:', query),
    'play_similar' => 'Finding similar tracks',
    'steer_mood' => withQ('Steering the mood toward', _arg(args, 'mood')),
    'search_by_acoustic_profile' => 'Matching tracks by sound',
    'playback_control' => switch (_arg(args, 'action')) {
      '' => 'Adjusting playback',
      final a => 'Playback: $a',
    },
    'save_queue_as_playlist' =>
      nameArg.isEmpty
          ? 'Saving the queue'
          : withQ('Saving the queue as', nameArg),
    'get_player_status' => 'Checking the player',
    'get_current_track_info' => 'Reading the current track',
    'search_online' =>
      query.isEmpty
          ? 'Searching online stores'
          : withQ('Searching online for', query),
    'web_search' =>
      query.isEmpty
          ? 'Searching the web'
          : withQ('Searching the web for', query),
    'learn_about' =>
      nameArg.isEmpty
          ? 'Looking up what’s playing'
          : withQ('Reading about', nameArg),
    'find_related_artists' =>
      artist.isEmpty
          ? 'Finding related artists'
          : withQ('Finding artists like', artist),
    'get_library_overview' => 'Surveying your library',
    'get_top_played' => 'Checking your most played',
    'list_artists' => 'Listing artists',
    'get_artist_albums' =>
      artist.isEmpty ? 'Listing albums' : withQ('Listing albums by', artist),
    'get_album_tracks' =>
      album.isEmpty ? 'Listing album tracks' : withQ('Opening', album),
    'list_playlists' => 'Listing playlists',
    'get_playlist_tracks' =>
      nameArg.isEmpty
          ? 'Opening a playlist'
          : withQ('Opening playlist', nameArg),
    'get_recently_played' => 'Checking recent plays',
    'get_track_details' => withQ('Reading track details for', query),
    'play_album' =>
      album.isEmpty ? 'Playing an album' : withQ('Playing', album),
    'play_playlist' =>
      nameArg.isEmpty
          ? 'Playing a playlist'
          : withQ('Playing playlist', nameArg),
    'get_metadata_status' => 'Checking metadata coverage',
    'get_artist_metadata' =>
      artist.isEmpty
          ? 'Reading artist metadata'
          : withQ('Reading metadata for', artist),
    'research_artist_metadata' =>
      artist.isEmpty
          ? 'Researching artist metadata'
          : withQ('Researching', artist),
    'apply_artist_metadata' => 'Saving artist metadata',
    _ => name.replaceAll('_', ' '),
  };
}

/// A short note from a tool's result dict: its error, or a result count.
String toolSummary(Map<String, Object?> result) {
  if (result['success'] == false) {
    final err = result['error'];
    if (err is String && err.isNotEmpty) {
      final r = err.runes;
      return r.length > 80 ? '${String.fromCharCodes(r.take(79))}…' : err;
    }
    return 'Failed';
  }
  final count = result['count'];
  if (count is int) return count == 1 ? '1 result' : '$count results';
  return '';
}
