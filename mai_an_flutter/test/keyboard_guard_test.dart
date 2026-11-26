import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/ui/keyboard_guard.dart';

void main() {
  Future<void> pushThenPop(WidgetTester tester, {required bool guarded}) async {
    final nav = GlobalKey<NavigatorState>();
    await tester.pumpWidget(
      MaterialApp(
        navigatorKey: nav,
        navigatorObservers: [if (guarded) KeyboardGuard()],
        home: const Scaffold(body: TextField()),
      ),
    );
    await tester.tap(find.byType(TextField));
    await tester.pump();
    expect(tester.testTextInput.isVisible, isTrue);

    nav.currentState!.push(
      MaterialPageRoute<void>(builder: (_) => const Scaffold()),
    );
    await tester.pumpAndSettle();
    nav.currentState!.pop();
    await tester.pumpAndSettle();
  }

  testWidgets('without the guard, popping restores focus (the bug)', (
    tester,
  ) async {
    await pushThenPop(tester, guarded: false);
    expect(isTextFocus(FocusManager.instance.primaryFocus!), isTrue);
    expect(tester.testTextInput.isVisible, isTrue);
  });

  testWidgets('with the guard, popping never refocuses the text field', (
    tester,
  ) async {
    await pushThenPop(tester, guarded: true);
    expect(isTextFocus(FocusManager.instance.primaryFocus!), isFalse);
    expect(tester.testTextInput.isVisible, isFalse);
  });

  testWidgets('dialogs still autofocus their own field', (tester) async {
    final nav = GlobalKey<NavigatorState>();
    await tester.pumpWidget(
      MaterialApp(
        navigatorKey: nav,
        navigatorObservers: [KeyboardGuard()],
        home: const Scaffold(body: TextField()),
      ),
    );
    await tester.tap(find.byType(TextField));
    await tester.pump();
    showDialog<void>(
      context: nav.currentContext!,
      builder: (_) => const AlertDialog(content: TextField(autofocus: true)),
    );
    await tester.pumpAndSettle();
    final focused = FocusManager.instance.primaryFocus!;
    expect(isTextFocus(focused), isTrue);
    expect(
      focused.context!.findAncestorWidgetOfExactType<AlertDialog>(),
      isNotNull,
    );
  });
}
