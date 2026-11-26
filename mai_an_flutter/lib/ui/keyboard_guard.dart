import 'package:flutter/widgets.dart';

/// Keeps the soft keyboard tied to the user touching a text field.
///
/// Flutter remembers the last focused node of every focus scope and hands
/// focus back to it when that scope is shown again: popping a route refocuses
/// whatever the route underneath last had focused. If that was a text field
/// (Library's search bar, say, before Settings was opened), the keyboard
/// pops up on its own with nothing typed. Dropping text focus *before* the
/// scope is left clears that history, so nothing is restored automatically.

/// Unfocuses the primary focus if it is a text field. The default
/// [UnfocusDisposition.scope] also clears the enclosing scope's focus
/// history, so the field can't be refocused by a later restore.
void dropTextFocus() {
  final node = FocusManager.instance.primaryFocus;
  if (node != null && isTextFocus(node)) node.unfocus();
}

/// Whether [node] belongs to a text field ([EditableText] attaches it to a
/// `Focus` of its own, so the context sits just below the field).
bool isTextFocus(FocusNode node) {
  final ctx = node.context;
  if (ctx == null || !ctx.mounted) return false;
  return ctx.widget is EditableText ||
      ctx.findAncestorStateOfType<EditableTextState>() != null;
}

/// Drops text focus whenever a route goes on top of the current one (pages,
/// dialogs, sheets, menus), so popping it back never reopens the keyboard.
class KeyboardGuard extends NavigatorObserver {
  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    if (previousRoute != null) dropTextFocus();
  }

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) {
    dropTextFocus();
  }
}
