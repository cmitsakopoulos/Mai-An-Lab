/// Port of `StreamripApp/utils/exceptions.py`.
///
/// Exceptions raised by streaming source clients (Qobuz, Deezer).
sealed class SourceException implements Exception {
  const SourceException([this.message]);

  final String? message;

  @override
  String toString() => message == null || message!.isEmpty
      ? '$runtimeType'
      : '$runtimeType: $message';
}

class AuthenticationError extends SourceException {
  const AuthenticationError([super.message]);
}

class MissingCredentialsError extends SourceException {
  const MissingCredentialsError([super.message]);
}

class IneligibleError extends SourceException {
  const IneligibleError([super.message]);
}

class InvalidAppIdError extends SourceException {
  const InvalidAppIdError([super.message]);
}

class InvalidAppSecretError extends SourceException {
  const InvalidAppSecretError([super.message]);
}

class NonStreamableError extends SourceException {
  const NonStreamableError([super.message]);

  String printMsg(Object item) {
    final parts = <String>['Unable to stream $item.'];
    if (message != null && message!.isNotEmpty) {
      parts.addAll(['Message:', message!]);
    }
    return parts.join(' ');
  }
}

class ConversionError extends SourceException {
  const ConversionError([super.message]);
}
