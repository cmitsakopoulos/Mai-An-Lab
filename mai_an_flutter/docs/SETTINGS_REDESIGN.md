# Settings redesign: handoff plan

Every Settings page is being rebuilt on one shared kit,
[`lib/settings/settings_kit.dart`](../lib/settings/settings_kit.dart), so the
pages share one look. This file is the plan and the record of progress.
Update the status table as you finish steps.

## Why

Before the redesign, Settings had three overlapping widget systems:
- the Flet-port leftovers (`SettingsCard`, `CardTitle`, `SwitchRow`,
  `SettingsDropdown`);
- a partly adopted grouped-inset style (`SettingsSection`, `InsetSwitchRow`,
  `SettingsPickerRow`);
- one-off widgets on individual pages.

On top of that:
- Four different switch setups used Material 3 defaults, which drew a dark
  thumb.
- The navigation segmented control was reused inside cards.
- Some pages saved as you changed them and others needed a Save button.

## Agreed rules

1. **One kit.** Pages build only from the widgets in `settings_kit.dart`, plus
   `GroupBody` for the scrolling page. Don't style rows inside a page; if a
   page needs something new, add it to the kit.
2. **Custom panels are allowed.** Content that isn't a row (EQ curve, sync
   status, glass preview, swatches) goes in a `SettingsCustomTile` inside a
   section, so it gets the same padding. Don't force it into a row.
3. **Saving.** Toggles, pickers, segmented controls and sliders apply
   immediately. An explicit Save is kept only where text is typed as a whole:
   credentials (Account, the Assistant API key) and the raw TOML editor. Put
   Save as a `SettingsTile.action` row inside the form's own section. Don't
   use the floating `GroupBody.onSave` button; remove it once nothing uses it.
4. **Destructive actions** use `SettingsTile.action(destructive: true)` and
   confirm first (`confirmDelete`).
5. **Migrate one page at a time.** After each step:
   - run `flutter analyze` and `flutter test`;
   - build (see Shipping);
   - the user checks it on the phone;
   - commit on `flutter-port`.

## Kit reference (`lib/settings/settings_kit.dart`)

| Widget | Use |
|---|---|
| `SettingsSection(header:, footer:, headerTrailing:, children:)` | Uppercase accent header, one card of rows with hairline dividers added automatically (indented past the icon when the row has a `leading`), optional footnote. |
| `SettingsTile(title:, subtitle:, leading:, trailing:, onTap:)` | The base row. With no `onTap` it is informational. |
| `SettingsTile.toggle(value:, onChanged:)` | `AppSwitch`; tapping the whole row toggles it. |
| `SettingsTile.nav(value:, onTap:)` | Chevron, optional grey value text. Opens a sub-page, sheet or dialog. |
| `SettingsTile.action(destructive:, onTap:)` | Accent-coloured (or red) title, no trailing control. |
| `SettingsIcon(icon, color:)` | Tinted rounded-square leading badge. |
| `SettingsPickerTile<T>(value:, options:, onChanged:)` | Shows the current choice; opens `showSettingsPicker` (a frosted sheet with a check on the current choice). Replaces `SettingsDropdown`. |
| `SettingsSegmentedTile<T>(inline:)` / `SettingsSegmented<T>` | Neutral grey segmented control, 2–3 short options only. With `inline: true` it sits beside the title; with `false` it goes full width underneath. Use a picker for anything longer. |
| `SettingsSliderTile(value:, valueLabel:, marks:)` | Title and value on one line, slider below, optional end labels. |
| `SettingsCustomTile(title:, subtitle:, child:)` | Any panel, with the rows' padding. |
| `SettingsOption<T>(value, label, subtitle:, icon:)` | One option for pickers and segmented controls. |
| `SettingsStyle` | Type and spacing constants (title, subtitle, value, footer). |

Page shell: `GroupBody(spacing: 26, children: [sections…])`.
`AppSwitch` (`lib/ui/app_switch.dart`) is the only switch in the app.
`Tokens.theme()` also styles any plain Material `Switch` as a fallback.

## Status

| # | Step | Status | Commit |
|---|---|---|---|
| 1 | `AppSwitch` everywhere, switch theme, `GlassCard` always provides a `Material` (ripples show) | done | a0c5f57 |
| 2 | Kit, plus Appearance rebuilt | done | 431c484 |
| 3 | Haptics, Permissions, Advanced, About | done | see `git log` |
| 4 | Storage & Paths, Database | todo | |
| 5 | Audio & DSP | todo | |
| 6 | Account, AI Assistant | todo | |
| 7 | Companion & Sync | todo | |
| 8 | Cleanup and row-level search | todo | |

## Remaining steps in detail

### 4. Storage & Paths, Database (`groups.dart`)
Storage (`_StorageGroupState`):
- **Download/library folders:** currently two `SettingsCard`s, each with a
  `TextField` and a browse button, saved through `GroupBody.onSave`. Make
  them `SettingsTile.nav` rows (title, path as the subtitle) that open the
  existing folder browser (`_browse`). Apply the change as soon as a folder
  is picked (`_picked`) and drop the Save button.
- **Default quality:** becomes a `SettingsPickerTile` over
  `kDefaultQualityOptions`, applied immediately.

Database (`_DatabaseGroupState`):
- **Maintenance buttons:** `TextButton.icon`s → `SettingsTile.action`; keep
  `_confirmThen`, and mark the wipe actions `destructive: true`.
- **Compute pipeline buttons:** become rows too.
- **Progress:** the progress bar and status (`LinearProgressIndicator` with
  `analysis.status`) go in a `SettingsCustomTile` in the same section, shown
  while a job runs.
- **Kit gap:** the page's intro `Dim` texts become section footers. If a
  maintenance row needs a disabled state while a job runs, add `enabled:` to
  `SettingsTile.action`.

Then delete `SettingsDropdown` and `_fieldDecoration` if nothing else uses
them.

### 5. Audio & DSP (`audio_dsp.dart`)
- It uses `LegacySettingsSection` and `InsetSwitchRow`. Move it to
  `SettingsSection` with `SettingsTile.toggle`.
- The EQ curve and band sliders are a `SettingsCustomTile`; keep their
  drawing as is.
- Presets become a `SettingsPickerTile`, or nav rows to their own sheet.
- `settings_test.dart` "Audio & DSP" taps the texts
  'Enable 5-Band Equalizer' and 'Custom Presets'. Keep those strings or
  update the test.

### 6. Account (`auth_settings.dart`), AI Assistant (`assistant_settings.dart`)
- Credentials stay explicit-save (rule 3): `ModernTextField`s in a
  `SettingsCustomTile`, then a 'Save' `SettingsTile.action` row in the same
  section.
- Remove `GroupBody.onSave` from both pages.
- The Account token switch row becomes `SettingsTile.toggle`.
- Assistant uses `SettingsPickerRow` (provider, model) → `SettingsPickerTile`.
  Its own toggles → `SettingsTile.toggle`.
- Tests: `assistant_ui_test.dart` and `settings_test.dart` ("Authentication
  saves Qobuz and Deezer credentials independently") look up labels and the
  save button by their text. Update them with the UI.

### 7. Companion & Sync (`sync_hub.dart`)
- It has its own `card([...])` helper over `SettingsCard`, and a custom
  `head(...)` row.
- The connection status (coloured dot, peer name) is a `SettingsCustomTile`
  or a `SettingsTile` with a custom `trailing`.
- The `sync-switch` and `ask-switch` `AppSwitch` keys must survive: tests and
  the interop test use them. Pass `key:` to `SettingsTile.toggle`; the key
  lands on the row, so check the finders still hit the switch, or put the
  key on the `AppSwitch` through a custom `trailing`.
- App Updates and the Report dialog: action and nav rows.

### 8. Cleanup
- Delete what nothing uses any more: `SettingsCard`, `LegacySettingsSection`,
  `InsetSwitchRow`, `SettingsPickerRow`, `SettingsDropdown`, `AccentLabel`
  (still used by the hub in `settings_page.dart`), and `GroupBody.onSave`
  with its floating `OnyxButton`.
- Optionally rebuild the hub (`_Hub` / `_HubItem` in `settings_page.dart`) on
  `SettingsSection` + `SettingsTile.nav` with `SettingsIcon`. It already
  looks close, so keep the highlight pulse.
- **Row-level search:** give rows stable ids, and extend
  `settings_search.dart` entries so a result opens the page and scrolls to
  and pulses the row. The hub already does this for pages with
  `Scrollable.ensureVisible` and `_pulse`.
- Search: `test/goldens/settings_search.json` must stay in sync.

## Shipping each step to the phone (maian)

```
cd mai_an_flutter
flutter build apk --release --target-platform android-arm64
```

The Mac companion (`mai_an_mac`, "Mai An Companion.app") watches
`build/app/outputs/flutter-apk/app-release.apk` and offers each new build to
the phone. Companion Sync must be on in the phone app. The companion must be
running: open the app, or run `./build/maian`. You can also use the control
page's "Build & offer to phone" button. See `mai_an_mac/README.md`.

## Known pre-existing test failures (not caused by this work)

- `settings_search_test.dart` "every query lists what Flet lists"
- `download_dock_test.dart` "pill, sheet and the downloads notification
  target"
- `agent_progress_test.dart` "streamed text shows as a draft…" (flaky)

## Checking layout without a device

Temporarily put a test under `test/_tmp/` that pumps the kit widgets under
`Tokens.theme()`, and run it with `--update-goldens` and
`matchesGoldenFile('x.png')`. Look at the PNG (the test font draws text as
blocks), then delete `test/_tmp/`.
