import 'companion_client.dart' show FeatureRow;

/// What the DSP offload needs from the library (sync_hub.py's
/// `db_manager` calls and the graph rebuild after). LibraryController is
/// the app's.
abstract interface class OffloadLibrary {
  /// `get_tracks_missing_features`; null while no library is open.
  Future<List<String>?> tracksMissingFeatures();

  /// `update_tracks_features_batch`: one batch of the Mac's features.
  Future<void> storeFeatureRows(List<FeatureRow> rows);

  /// `build_metadata_edges`, `build_acoustic_edges`, then the library
  /// reloaded.
  Future<void> rebuildSimilarityGraph();
}
