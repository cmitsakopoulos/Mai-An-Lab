import 'package:flutter/foundation.dart';

/// Default verified Android Qobuz API pair used by the Flet app.
const kQobuzDefaultAppId = '312369995';
const kQobuzDefaultSecret = 'e79f8b9be485692b0e5f9dd895826368';

/// `[qobuz]` section of `config.toml` (`utils/config.py` `QobuzConfig`).
class QobuzConfig {
  const QobuzConfig({
    this.useAuthToken = false,
    this.emailOrUserid = '',
    this.passwordOrToken = '',
    this.appId = '',
    this.quality = 3,
    this.downloadBooklets = true,
    this.secrets = const [],
  });

  final bool useAuthToken;
  final String emailOrUserid;
  final String passwordOrToken;
  final String appId;
  final int quality;
  final bool downloadBooklets;
  final List<String> secrets;

  bool get hasCredentials =>
      emailOrUserid.trim().isNotEmpty && passwordOrToken.trim().isNotEmpty;

  String get effectiveAppId =>
      appId.trim().isNotEmpty ? appId.trim() : kQobuzDefaultAppId;

  String get effectiveSecret =>
      secrets.isNotEmpty && secrets.first.trim().isNotEmpty
      ? secrets.first.trim()
      : kQobuzDefaultSecret;

  static QobuzConfig fromToml(Object? section) {
    final m = section is Map ? section : const <Object?, Object?>{};
    final rawSecrets = m['secrets'];
    final secrets = <String>[
      if (rawSecrets is List)
        for (final s in rawSecrets)
          if (s != null && '$s'.isNotEmpty) '$s',
    ];
    return QobuzConfig(
      useAuthToken: _asBool(m['use_auth_token'], false),
      emailOrUserid: _asStr(m['email_or_userid'], ''),
      passwordOrToken: _asStr(m['password_or_token'], ''),
      appId: _asStr(m['app_id'], ''),
      quality: _asInt(m['quality'], 3),
      downloadBooklets: _asBool(m['download_booklets'], true),
      secrets: secrets,
    );
  }

  @override
  bool operator ==(Object other) =>
      other is QobuzConfig &&
      other.useAuthToken == useAuthToken &&
      other.emailOrUserid == emailOrUserid &&
      other.passwordOrToken == passwordOrToken &&
      other.appId == appId &&
      other.quality == quality &&
      other.downloadBooklets == downloadBooklets &&
      listEquals(other.secrets, secrets);

  @override
  int get hashCode => Object.hash(
    useAuthToken,
    emailOrUserid,
    passwordOrToken,
    appId,
    quality,
    downloadBooklets,
    Object.hashAll(secrets),
  );
}

/// `[deezer]` section of `config.toml` (`utils/config.py` `DeezerConfig`).
class DeezerConfig {
  const DeezerConfig({
    this.arl = '',
    this.quality = 2,
    this.lowerQualityIfNotAvailable = true,
  });

  final String arl;
  final int quality;
  final bool lowerQualityIfNotAvailable;

  bool get hasCredentials => arl.trim().isNotEmpty;

  static DeezerConfig fromToml(Object? section) {
    final m = section is Map ? section : const <Object?, Object?>{};
    return DeezerConfig(
      arl: _asStr(m['arl'], ''),
      quality: _asInt(m['quality'], 2),
      lowerQualityIfNotAvailable: _asBool(
        m['lower_quality_if_not_available'],
        true,
      ),
    );
  }

  @override
  bool operator ==(Object other) =>
      other is DeezerConfig &&
      other.arl == arl &&
      other.quality == quality &&
      other.lowerQualityIfNotAvailable == lowerQualityIfNotAvailable;

  @override
  int get hashCode => Object.hash(arl, quality, lowerQualityIfNotAvailable);
}

/// `[downloads]` client settings (`utils/config.py` `DownloadsConfig`).
class DownloadsClientConfig {
  const DownloadsClientConfig({
    this.folder = '',
    this.maxConnections = 6,
    this.requestsPerMinute = 60,
    this.verifySsl = true,
  });

  final String folder;
  final int maxConnections;
  final int requestsPerMinute;
  final bool verifySsl;

  static DownloadsClientConfig fromToml(Object? section) {
    final m = section is Map ? section : const <Object?, Object?>{};
    return DownloadsClientConfig(
      folder: _asStr(m['folder'], ''),
      maxConnections: _asInt(m['max_connections'], 6),
      requestsPerMinute: _asInt(m['requests_per_minute'], 60),
      verifySsl: _asBool(m['verify_ssl'], true),
    );
  }

  @override
  bool operator ==(Object other) =>
      other is DownloadsClientConfig &&
      other.folder == folder &&
      other.maxConnections == maxConnections &&
      other.requestsPerMinute == requestsPerMinute &&
      other.verifySsl == verifySsl;

  @override
  int get hashCode =>
      Object.hash(folder, maxConnections, requestsPerMinute, verifySsl);
}

/// Bundled source configuration passed to [QobuzClient], [DeezerClient], and
/// download/search entry points.
class SourceConfig {
  const SourceConfig({
    this.qobuz = const QobuzConfig(),
    this.deezer = const DeezerConfig(),
    this.downloads = const DownloadsClientConfig(),
    this.defaultQuality = '',
  });

  final QobuzConfig qobuz;
  final DeezerConfig deezer;
  final DownloadsClientConfig downloads;

  /// `general.default_quality`: `""` (ask), `"mp3"`, `"cd"`, or `"hires"`.
  final String defaultQuality;

  static SourceConfig fromTomlMap(Map<String, Object?> doc) {
    final general = doc['general'];
    final dq = general is Map ? _asStr(general['default_quality'], '') : '';
    return SourceConfig(
      qobuz: QobuzConfig.fromToml(doc['qobuz']),
      deezer: DeezerConfig.fromToml(doc['deezer']),
      downloads: DownloadsClientConfig.fromToml(doc['downloads']),
      defaultQuality: dq.toLowerCase(),
    );
  }

  @override
  bool operator ==(Object other) =>
      other is SourceConfig &&
      other.qobuz == qobuz &&
      other.deezer == deezer &&
      other.downloads == downloads &&
      other.defaultQuality == defaultQuality;

  @override
  int get hashCode => Object.hash(qobuz, deezer, downloads, defaultQuality);
}

String _asStr(Object? v, String fallback) => switch (v) {
  null => fallback,
  String s => s,
  final Object o => '$o',
};

int _asInt(Object? v, int fallback) => switch (v) {
  int i => i,
  num n => n.toInt(),
  String s => int.tryParse(s) ?? fallback,
  _ => fallback,
};

bool _asBool(Object? v, bool fallback) => switch (v) {
  null => fallback,
  bool b => b,
  num n => n != 0,
  String s => const {'true', '1', 'yes'}.contains(s.trim().toLowerCase()),
  _ => fallback,
};
