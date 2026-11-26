import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../ui/tokens.dart';
import 'app_update.dart';

/// What the update looks like right now: the line under the title, and
/// whether the progress bar shows.
String appUpdateStatusText(AppUpdater u) {
  final b = u.build;
  final mine = b == null ? '' : 'This phone runs ${b.label}.';
  return switch (u.state) {
    AppUpdateState.none => 'No update from the Mac. $mine',
    AppUpdateState.upToDate =>
      'Up to date: the Mac offers the build you run ($mine).',
    AppUpdateState.available =>
      '${u.isRebuild ? 'A rebuild of' : 'Mai An'} ${u.offerLabel} '
          '(${formatBytes(u.offerSize)}${u.offerIsDebug ? ', debug build' : ''}) '
          'is ready on your Mac. $mine',
    AppUpdateState.downloading =>
      u.progress == null
          ? 'Downloading from the Mac…'
          : 'Downloading from the Mac… ${(u.progress! * 100).round()}%',
    AppUpdateState.verifying => 'Checking the download…',
    AppUpdateState.needsPermission =>
      'Android needs your OK to let Mai An install updates: turn on '
          '"Allow from this source", then come back.',
    AppUpdateState.installing =>
      'Installing. Confirm on the Android screen; the app closes and a '
          'notification reopens it.',
    AppUpdateState.failed => u.error.isEmpty ? 'Update failed.' : u.error,
  };
}

/// The progress bar, the status line, and the buttons that fit the state.
class AppUpdatePanel extends StatelessWidget {
  const AppUpdatePanel({
    super.key,
    required this.updater,
    this.compact = false,
  });
  final AppUpdater updater;

  /// Inside the dialog, which brings its own buttons.
  final bool compact;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: updater,
      builder: (context, _) {
        final u = updater;
        final busy =
            u.state == AppUpdateState.downloading ||
            u.state == AppUpdateState.verifying;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              appUpdateStatusText(u),
              style: TextStyle(
                color: u.state == AppUpdateState.failed
                    ? Tokens.accentRed
                    : Tokens.dim,
                fontSize: 13,
              ),
            ),
            if (u.notes.isNotEmpty && _showsNotes(u.state)) ...[
              const SizedBox(height: 10),
              AppUpdateNotes(notes: u.notes, compact: compact),
            ],
            if (busy) ...[
              const SizedBox(height: 10),
              ClipRRect(
                borderRadius: BorderRadius.circular(2),
                child: LinearProgressIndicator(
                  value: u.progress,
                  minHeight: 4,
                  color: Tokens.accent,
                  backgroundColor: Tokens.surface2,
                ),
              ),
            ],
            if (!compact) ...[
              const SizedBox(height: 10),
              AppUpdateActions(updater: u),
            ],
          ],
        );
      },
    );
  }
}

bool _showsNotes(AppUpdateState s) => switch (s) {
  AppUpdateState.none || AppUpdateState.upToDate => false,
  _ => true,
};

/// "What's new", as written on the Mac when the build was made.
class AppUpdateNotes extends StatelessWidget {
  const AppUpdateNotes({super.key, required this.notes, this.compact = false});
  final String notes;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          const Text(
            "What's new",
            style: TextStyle(
              color: Tokens.text,
              fontSize: 12.5,
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 4),
          ConstrainedBox(
            constraints: BoxConstraints(maxHeight: compact ? 160 : 240),
            child: SingleChildScrollView(
              child: Text(
                notes,
                style: const TextStyle(
                  color: Tokens.dim,
                  fontSize: 13,
                  height: 1.4,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class AppUpdateActions extends StatelessWidget {
  const AppUpdateActions({super.key, required this.updater, this.onDone});
  final AppUpdater updater;

  /// Called after a choice that ends the dialog (Not now).
  final VoidCallback? onDone;

  @override
  Widget build(BuildContext context) {
    final u = updater;
    final pill = FilledButton.styleFrom(
      backgroundColor: Tokens.accent,
      foregroundColor: Tokens.bg,
    );
    final buttons = <Widget>[
      switch (u.state) {
        AppUpdateState.available || AppUpdateState.failed => FilledButton.icon(
          key: const ValueKey('app-update-go'),
          style: pill,
          icon: const Icon(Icons.system_update_rounded, size: 18),
          label: Text(
            u.state == AppUpdateState.failed ? 'Try again' : 'Update now',
          ),
          onPressed: u.offer == null ? null : u.update,
        ),
        AppUpdateState.needsPermission => FilledButton.icon(
          style: pill,
          icon: const Icon(Icons.admin_panel_settings_rounded, size: 18),
          label: const Text('Allow installs'),
          onPressed: u.allowInstalls,
        ),
        AppUpdateState.downloading => TextButton.icon(
          icon: const Icon(Icons.cancel_rounded, size: 18),
          label: const Text('Cancel'),
          onPressed: u.cancel,
        ),
        _ => const SizedBox.shrink(),
      },
      if (onDone != null && u.state == AppUpdateState.available)
        TextButton(
          onPressed: () {
            u.decline();
            onDone!();
          },
          child: const Text('Not now'),
        ),
    ];
    return Wrap(spacing: 8, runSpacing: 4, children: buttons);
  }
}

/// Shown once per new build: what it is, and Update now / Not now.
class AppUpdateDialog extends StatelessWidget {
  const AppUpdateDialog({super.key, required this.updater});
  final AppUpdater updater;

  @override
  Widget build(BuildContext context) {
    final width = math.min(
      380.0,
      math.max(280.0, MediaQuery.sizeOf(context).width - 48),
    );
    return ListenableBuilder(
      listenable: updater,
      builder: (context, _) => AlertDialog(
        title: const Row(
          children: [
            Icon(Icons.system_update_rounded, color: Tokens.accent, size: 24),
            SizedBox(width: 8),
            Expanded(
              child: Text(
                'App update from your Mac',
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
              ),
            ),
          ],
        ),
        content: SizedBox(
          width: width,
          child: AppUpdatePanel(updater: updater, compact: true),
        ),
        actions: [
          if (updater.state == AppUpdateState.installing ||
              updater.state == AppUpdateState.upToDate ||
              updater.state == AppUpdateState.none)
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Close'),
            )
          else
            AppUpdateActions(
              updater: updater,
              onDone: () => Navigator.pop(context),
            ),
        ],
      ),
    );
  }
}
