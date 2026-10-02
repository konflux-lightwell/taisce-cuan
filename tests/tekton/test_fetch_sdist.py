from unittest.mock import MagicMock, patch

from taisce_cuan.tekton.fetch_sdist import main


def test_writes_canonical_result_path(tmp_path):
    result_file = tmp_path / "result.txt"

    with patch("taisce_cuan.tekton.fetch_sdist.SdistSourceFetcher") as MockFetcher:
        MockFetcher.return_value.fetch.return_value = (MagicMock(), MagicMock(), None)

        rc = main(
            [
                "sniffio",
                "1.3.1",
                "--output-dir",
                str(tmp_path / "sdists-repo"),
                "--result-path",
                str(result_file),
            ]
        )

    assert rc == 0
    assert result_file.read_text() == "downloads/sniffio-1.3.1.tar.gz"


def test_normalizes_package_name_in_result(tmp_path):
    result_file = tmp_path / "result.txt"

    with patch("taisce_cuan.tekton.fetch_sdist.SdistSourceFetcher") as MockFetcher:
        MockFetcher.return_value.fetch.return_value = (MagicMock(), MagicMock(), None)

        rc = main(
            [
                "My.Package",
                "2.0.0",
                "--output-dir",
                str(tmp_path / "sdists-repo"),
                "--result-path",
                str(result_file),
            ]
        )

    assert rc == 0
    assert result_file.read_text() == "downloads/my-package-2.0.0.tar.gz"


def test_returns_1_on_fetch_failure(tmp_path):
    with patch("taisce_cuan.tekton.fetch_sdist.SdistSourceFetcher") as MockFetcher:
        MockFetcher.return_value.fetch.side_effect = RuntimeError("not found")

        rc = main(["sniffio", "1.3.1", "--output-dir", str(tmp_path)])

    assert rc == 1


def test_succeeds_without_result_path(tmp_path):
    with patch("taisce_cuan.tekton.fetch_sdist.SdistSourceFetcher") as MockFetcher:
        MockFetcher.return_value.fetch.return_value = (MagicMock(), MagicMock(), None)

        rc = main(["sniffio", "1.3.1", "--output-dir", str(tmp_path)])

    assert rc == 0
