import 'package:flutter/material.dart';

import '../sources/source_config.dart';
import '../ui/tokens.dart';
import 'settings_kit.dart';
import 'settings_page.dart';

/// Port of `StreamripApp/ui/views/settings.py` `_build_auth_group` and
/// `_save_account_credentials`. Each source is its own form with its own
/// Save row; the session-token switch applies at once.
class AuthGroup extends StatefulWidget {
  const AuthGroup({super.key, required this.deps});

  final SettingsDeps deps;

  @override
  State<AuthGroup> createState() => _AuthGroupState();
}

class _AuthGroupState extends State<AuthGroup> {
  final _uid = TextEditingController();
  final _token = TextEditingController();
  final _appId = TextEditingController();
  final _appSecret = TextEditingController();
  final _arl = TextEditingController();
  bool _useToken = true;

  String _baseUid = '';
  String _baseToken = '';
  String _baseAppId = '';
  String _baseAppSecret = '';
  String _baseArl = '';

  SettingsDeps get d => widget.deps;

  @override
  void initState() {
    super.initState();
    _loadFromConfig();
    for (final c in [_uid, _token, _appId, _appSecret, _arl]) {
      c.addListener(_changed);
    }
  }

  @override
  void dispose() {
    for (final c in [_uid, _token, _appId, _appSecret, _arl]) {
      c.dispose();
    }
    super.dispose();
  }

  void _loadFromConfig() {
    final q = d.config.config.sources.qobuz;
    final dz = d.config.config.sources.deezer;
    final rawQobuz = d.config.raw['qobuz'];
    final hasUseTokenKey =
        rawQobuz is Map && rawQobuz.containsKey('use_auth_token');

    _uid.text = _baseUid = q.emailOrUserid;
    _token.text = _baseToken = q.passwordOrToken;
    _appId.text = _baseAppId = q.appId.isNotEmpty
        ? q.appId
        : kQobuzDefaultAppId;
    _appSecret.text = _baseAppSecret = q.secrets.isNotEmpty
        ? q.secrets.first
        : kQobuzDefaultSecret;
    _useToken = hasUseTokenKey ? q.useAuthToken : true;
    _arl.text = _baseArl = dz.arl;
  }

  void _changed() => setState(() {});

  bool get _qobuzDirty =>
      _uid.text != _baseUid ||
      _token.text != _baseToken ||
      _appId.text != _baseAppId ||
      _appSecret.text != _baseAppSecret;

  bool get _deezerDirty => _arl.text != _baseArl;

  Future<void> _onUseToken(bool v) async {
    setState(() => _useToken = v);
    await d.config.update({
      'qobuz': {'use_auth_token': v},
    });
  }

  Future<void> _saveQobuz() async {
    final uid = _uid.text.trim();
    final token = _token.text.trim();
    if (uid.isEmpty || token.isEmpty) {
      showSettingsSnack(context, 'Qobuz needs both a User ID and a token');
      return;
    }
    final appId = _appId.text.trim().isNotEmpty
        ? _appId.text.trim()
        : kQobuzDefaultAppId;
    final appSecret = _appSecret.text.trim().isNotEmpty
        ? _appSecret.text.trim()
        : kQobuzDefaultSecret;
    final ok = await d.config.update({
      'qobuz': {
        'use_auth_token': _useToken,
        'email_or_userid': uid,
        'password_or_token': token,
        'app_id': appId,
        'secrets': [appSecret],
      },
    });
    if (!mounted) return;
    if (!ok) {
      showSettingsSnack(context, 'Qobuz save failed');
      return;
    }
    setState(() {
      _baseUid = _uid.text;
      _baseToken = _token.text;
      _baseAppId = _appId.text;
      _baseAppSecret = _appSecret.text;
    });
    showSettingsSnack(context, 'Qobuz credentials updated.');
  }

  Future<void> _saveDeezer() async {
    final arl = _arl.text.trim();
    final hadArl = _baseArl.trim().isNotEmpty;
    final ok = await d.config.update({
      'deezer': {'arl': arl},
    });
    if (!mounted) return;
    if (!ok) {
      showSettingsSnack(context, 'Deezer save failed');
      return;
    }
    setState(() => _baseArl = _arl.text);
    if (arl.isNotEmpty) {
      showSettingsSnack(context, 'Deezer credentials updated.');
    } else if (hadArl) {
      showSettingsSnack(context, 'Deezer ARL cleared.');
    }
  }

  @override
  Widget build(BuildContext context) {
    return GroupBody(
      spacing: 26,
      children: [
        SettingsAnchor(
          id: 'account.qobuz',
          child: SettingsSection(
            header: 'Qobuz Account',
            footer:
                'Authentication details used to generate valid Qobuz user '
                'sessions.',
            children: [
              SettingsCustomTile(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    ModernTextField(
                      fieldKey: const ValueKey('qobuz_uid'),
                      label: 'Qobuz User ID',
                      controller: _uid,
                      hint: 'e.g. 1234567',
                      help:
                          'Your registered Qobuz email address or numeric '
                          'User ID.',
                    ),
                    const SizedBox(height: 14),
                    ModernTextField(
                      fieldKey: const ValueKey('qobuz_token'),
                      label: 'Auth Token / Password Hash',
                      controller: _token,
                      hint: 'Enter token or MD5 of password',
                      help: 'Pre-hashed MD5 password string or session token.',
                      obscureToggle: true,
                    ),
                  ],
                ),
              ),
              SettingsAnchor(
                id: 'account.token',
                child: SettingsTile.toggle(
                  title: 'Use Session Token Authentication',
                  subtitle:
                      'Bypasses password authentication when a valid token is '
                      'present.',
                  value: _useToken,
                  onChanged: _onUseToken,
                ),
              ),
              SettingsTile.action(
                leading: const SettingsIcon(
                  Icons.key_rounded,
                  color: Tokens.qobuz,
                ),
                title: 'Save Qobuz',
                onTap: _qobuzDirty ? _saveQobuz : null,
              ),
            ],
          ),
        ),
        SettingsAnchor(
          id: 'account.qobuz_keys',
          child: SettingsSection(
            header: 'Qobuz Developer API Keys',
            footer:
                'Qobuz Application ID and Secret required to negotiate API '
                'sessions. Saved with Save Qobuz.',
            children: [
              SettingsCustomTile(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    ModernTextField(
                      fieldKey: const ValueKey('qobuz_app_id'),
                      label: 'Qobuz App ID',
                      controller: _appId,
                      hint: 'Default: $kQobuzDefaultAppId',
                      help:
                          'Application Identifier registered with the Qobuz '
                          'developer portal.',
                    ),
                    const SizedBox(height: 14),
                    ModernTextField(
                      fieldKey: const ValueKey('qobuz_app_secret'),
                      label: 'Qobuz App Secret',
                      controller: _appSecret,
                      hint: 'Default: $kQobuzDefaultSecret',
                      help: 'Application Secret key paired with your App ID.',
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        SettingsAnchor(
          id: 'account.deezer',
          child: SettingsSection(
            header: 'Deezer Account',
            footer:
                "FLAC needs a Deezer plan with lossless and 320 kbps MP3 a "
                "paid plan. If your plan can't serve the quality you pick, "
                'downloads drop to the best it can.',
            children: [
              SettingsCustomTile(
                child: ModernTextField(
                  fieldKey: const ValueKey('deezer_arl'),
                  label: 'Deezer ARL Cookie',
                  controller: _arl,
                  hint: 'Long alphanumeric string from your browser cookies',
                  help:
                      'Browser → DevTools → Application → '
                      'Cookies → deezer.com → copy the \'arl\' value.',
                  obscureToggle: true,
                ),
              ),
              SettingsTile.action(
                leading: const SettingsIcon(
                  Icons.key_rounded,
                  color: Tokens.deezer,
                ),
                title: 'Save Deezer',
                onTap: _deezerDirty ? _saveDeezer : null,
              ),
            ],
          ),
        ),
      ],
    );
  }
}
