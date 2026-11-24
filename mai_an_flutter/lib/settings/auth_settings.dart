import 'package:flutter/material.dart';

import '../sources/source_config.dart';
import '../ui/tokens.dart';
import 'settings_page.dart';

/// Port of `StreamripApp/ui/views/settings.py` `_build_auth_group` and
/// `_save_account_credentials`.
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
  bool _baseUseToken = true;

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
    _useToken = _baseUseToken = hasUseTokenKey ? q.useAuthToken : true;
    _arl.text = _baseArl = dz.arl;
  }

  void _changed() => setState(() {});

  bool get _dirty =>
      _uid.text != _baseUid ||
      _token.text != _baseToken ||
      _appId.text != _baseAppId ||
      _appSecret.text != _baseAppSecret ||
      _useToken != _baseUseToken ||
      _arl.text != _baseArl;

  Future<void> _save() async {
    final uid = _uid.text.trim();
    final token = _token.text.trim();
    final arl = _arl.text.trim();
    final hadArl = _baseArl.trim().isNotEmpty;

    final saved = <String>[];
    final problems = <String>[];

    if (uid.isNotEmpty && token.isNotEmpty) {
      final appId =
          _appId.text.trim().isNotEmpty ? _appId.text.trim() : kQobuzDefaultAppId;
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
      if (ok) {
        saved.add('Qobuz');
      } else {
        problems.add('Qobuz save failed');
      }
    } else if (uid.isNotEmpty || token.isNotEmpty) {
      problems.add('Qobuz needs both a User ID and a token');
    }

    final dzOk = await d.config.update({
      'deezer': {'arl': arl},
    });
    if (dzOk) {
      if (arl.isNotEmpty) saved.add('Deezer');
    } else {
      problems.add('Deezer save failed');
    }

    if (!mounted) return;
    setState(() {
      _baseUid = _uid.text;
      _baseToken = _token.text;
      _baseAppId = _appId.text;
      _baseAppSecret = _appSecret.text;
      _baseUseToken = _useToken;
      _baseArl = _arl.text;
    });

    if (problems.isNotEmpty) {
      final prefix = saved.isNotEmpty ? 'Saved ${saved.join(' & ')}. ' : '';
      showSettingsSnack(context, '$prefix${problems.join('; ')}');
    } else if (saved.isNotEmpty) {
      showSettingsSnack(context, '${saved.join(' & ')} credentials updated.');
    } else if (hadArl && arl.isEmpty) {
      showSettingsSnack(context, 'Deezer ARL cleared.');
    }
  }

  InputDecoration _decoration(
    BuildContext context, {
    required String label,
    required String hint,
    Color? focusColor,
  }) {
    final accent = focusColor ?? Theme.of(context).colorScheme.primary;
    OutlineInputBorder border(Color c) => OutlineInputBorder(
      borderRadius: BorderRadius.circular(10),
      borderSide: BorderSide(color: c),
    );
    return InputDecoration(
      labelText: label,
      hintText: hint,
      labelStyle: TextStyle(color: accent, fontSize: 11),
      hintStyle: const TextStyle(color: Tokens.dim, fontSize: 13),
      filled: true,
      fillColor: Tokens.surface2,
      enabledBorder: border(Tokens.border),
      focusedBorder: border(accent),
      border: border(Tokens.border),
      isDense: true,
    );
  }

  Widget _sourceHeader(IconData icon, Color color, String title) => Row(
    children: [
      Icon(icon, color: color, size: 18),
      const SizedBox(width: 8),
      Expanded(
        child: Text(
          title,
          style: const TextStyle(
            color: Tokens.text,
            fontSize: 13,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    ],
  );

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return GroupBody(
      onSave: _dirty ? _save : null,
      saveLabel: 'SAVE CREDENTIALS',
      saveIcon: Icons.key_rounded,
      children: [
        SettingsCard(
          child: Row(
            children: [
              Icon(Icons.lock_person_rounded, color: accent, size: 24),
              const SizedBox(width: 14),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Streaming Credentials',
                      style: TextStyle(
                        color: Tokens.text,
                        fontSize: 15,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    SizedBox(height: 2),
                    Dim(
                      'Sign in to each source you want to search and download '
                      'from. Both can be configured at once.',
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        SettingsCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _sourceHeader(
                Icons.account_circle_rounded,
                Tokens.qobuz,
                'Qobuz Account',
              ),
              const SizedBox(height: 8),
              const Dim(
                'Authentication details used to generate valid Qobuz user '
                'sessions.',
                size: 11,
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _uid,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
                decoration: _decoration(
                  context,
                  label: 'Qobuz User ID',
                  hint: 'e.g. 1234567',
                ),
              ),
              const SizedBox(height: 6),
              const Dim(
                'Your registered Qobuz email address or numeric User ID.',
                size: 11,
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _token,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
                decoration: _decoration(
                  context,
                  label: 'Auth Token / Password Hash',
                  hint: 'Enter token or MD5 of password',
                ),
              ),
              const SizedBox(height: 6),
              const Dim(
                'Pre-hashed MD5 password string or session token.',
                size: 11,
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Switch(
                    value: _useToken,
                    activeThumbColor: accent,
                    onChanged: (v) => setState(() => _useToken = v),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: GestureDetector(
                      onTap: () => setState(() => _useToken = !_useToken),
                      child: const Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Use Session Token Authentication',
                            style: TextStyle(
                              color: Tokens.text,
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          SizedBox(height: 1),
                          Dim(
                            'Bypasses password authentication when a valid '
                            'token is present.',
                            size: 11,
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
        SettingsCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _sourceHeader(
                Icons.key_rounded,
                Tokens.qobuz,
                'Qobuz Developer API Keys',
              ),
              const SizedBox(height: 8),
              const Dim(
                'Qobuz Application ID and Secret required to negotiate API '
                'sessions.',
                size: 11,
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _appId,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
                decoration: _decoration(
                  context,
                  label: 'Qobuz App ID',
                  hint: 'Default: $kQobuzDefaultAppId',
                ),
              ),
              const SizedBox(height: 6),
              const Dim(
                'Application Identifier registered with the Qobuz developer '
                'portal.',
                size: 11,
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _appSecret,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
                decoration: _decoration(
                  context,
                  label: 'Qobuz App Secret',
                  hint: 'Default: $kQobuzDefaultSecret',
                ),
              ),
              const SizedBox(height: 6),
              const Dim(
                'Application Secret key paired with your App ID.',
                size: 11,
              ),
            ],
          ),
        ),
        SettingsCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _sourceHeader(
                Icons.account_circle_rounded,
                Tokens.deezer,
                'Deezer Account',
              ),
              const SizedBox(height: 8),
              const Dim(
                'Deezer authenticates with a single ARL cookie taken from a '
                'logged-in browser session.',
                size: 11,
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _arl,
                style: const TextStyle(color: Tokens.text, fontSize: 13),
                decoration: _decoration(
                  context,
                  label: 'Deezer ARL Cookie',
                  hint: 'Long alphanumeric string from your browser cookies',
                  focusColor: Tokens.deezer,
                ),
              ),
              const SizedBox(height: 6),
              const Dim(
                'Browser \u2192 DevTools \u2192 Application \u2192 Cookies '
                '\u2192 deezer.com \u2192 copy the \'arl\' value.',
                size: 11,
              ),
              const SizedBox(height: 12),
              const Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.info_outline_rounded, color: Tokens.dim, size: 14),
                  SizedBox(width: 6),
                  Expanded(
                    child: Dim(
                      'FLAC needs a Deezer plan with lossless and 320 kbps MP3 '
                      'a paid plan. If your plan can\'t serve the quality you '
                      'pick, downloads drop to the best it can.',
                      size: 11,
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ],
    );
  }
}
