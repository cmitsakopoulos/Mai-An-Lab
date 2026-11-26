import 'dart:async';

import 'package:flutter/material.dart';

import '../library/library_controller.dart';
import '../player/queue_store.dart' show Prefs;
import '../ui/tokens.dart';
import 'app_update.dart';
import 'app_update_view.dart';
import 'sync_dialogs.dart';
import 'sync_manager.dart';

/// The app's side of the Companion manager (main.py's `app` as manager.py
/// sees it): flet_prefs.json, the lifecycle, snackbars, the PIN and
/// transfer dialogs on the root navigator, the download queue and the
/// features lookup.
class AppSyncHost implements SyncHost {
  AppSyncHost({
    required this.prefs,
    required this.navigator,
    required this.onSnack,
    required this.library,
    this.queue,
  });

  final Prefs prefs;
  final GlobalKey<NavigatorState> navigator;
  final void Function(String text, {IconData? icon, Color? color}) onSnack;
  final LibraryController library;

  @override
  final TransferQueue? queue;

  /// Set once the manager exists (it is built with this host).
  late CompanionSyncManager manager;

  /// main.py `is_background`, set by the app's lifecycle listener.
  @override
  bool inBackground = false;

  final _routes = <Object, Route<void>>{};

  @override
  Object? pref(String key) => prefs[key];

  /// main.py `_save_pref` returns once the value is set (its file write is
  /// synchronous); the manager doesn't wait for the disk either.
  @override
  Future<void> savePref(String key, Object? value) {
    unawaited(prefs.set(key, value));
    return Future.value();
  }

  static const _icons = {
    SyncIcon.linkOff: Icons.link_off_rounded,
    SyncIcon.check: Icons.check_circle,
    SyncIcon.download: Icons.download_rounded,
    SyncIcon.error: Icons.error_outline_rounded,
    SyncIcon.upload: Icons.upload_rounded,
    SyncIcon.sync: Icons.sync_rounded,
    SyncIcon.wifiOff: Icons.wifi_off_rounded,
    SyncIcon.radar: Icons.radar_rounded,
    SyncIcon.stop: Icons.stop_circle_rounded,
  };

  @override
  void snack(String text, {SyncIcon? icon, bool red = false}) => onSnack(
    text,
    icon: _icons[icon] ?? Icons.notifications_rounded,
    color: red ? Tokens.accentRed : null,
  );

  void _show(Object prompt, Widget dialog, VoidCallback closed) {
    final nav = navigator.currentState;
    if (nav == null) {
      closed();
      return;
    }
    final route = DialogRoute<void>(
      context: nav.context,
      builder: (_) => dialog,
    );
    _routes[prompt] = route;
    nav.push(route).whenComplete(() {
      _routes.remove(prompt);
      closed();
    });
  }

  void _dismiss(Object prompt, VoidCallback closed) {
    final route = _routes.remove(prompt);
    if (route != null && route.isActive) {
      final nav = route.navigator!;
      if (route.isCurrent) {
        nav.pop();
      } else {
        nav.removeRoute(route);
      }
    }
    closed();
  }

  @override
  void showPinPrompt(PinPrompt prompt) => _show(
    prompt,
    PinDialog(prompt: prompt),
    () => manager.pinPromptClosed(prompt),
  );

  @override
  void dismissPinPrompt(PinPrompt prompt) =>
      _dismiss(prompt, () => manager.pinPromptClosed(prompt));

  @override
  void showTransferPrompt(TransferPrompt prompt) => _show(
    prompt,
    TransferDialog(prompt: prompt),
    () => manager.transferPromptClosed(prompt),
  );

  @override
  void dismissTransferPrompt(TransferPrompt prompt) =>
      _dismiss(prompt, () => manager.transferPromptClosed(prompt));

  /// A new build of this app on the Mac: offer it, unless the app is in
  /// the background (the Companion & Sync card still shows it).
  void showAppUpdatePrompt(AppUpdater updater) {
    if (inBackground || _routes.containsKey(updater)) return;
    _show(updater, AppUpdateDialog(updater: updater), () {});
  }

  @override
  Future<Map<String, Object?>?> storedFeatures(String path) async =>
      library.repository?.storedFeatures(path);
}
