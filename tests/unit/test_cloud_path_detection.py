from ingest import is_cloud_sync_path


def test_cloud_sync_detection_onedrive():
    path = "/Users/alex/OneDrive/Docs/file.txt"
    assert is_cloud_sync_path(path) == "OneDrive"


def test_cloud_sync_detection_icloud():
    path = "/Users/alex/Library/Mobile Documents/com~apple~CloudDocs/file.txt"
    assert is_cloud_sync_path(path) == "iCloud"


def test_cloud_sync_detection_none():
    path = "/Users/alex/projects/local/file.txt"
    assert is_cloud_sync_path(path) is None
