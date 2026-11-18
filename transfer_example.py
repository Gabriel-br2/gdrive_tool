"""Manual examples for uploading and downloading files."""

from pathlib import Path

from gdrive_tool import download, upload


def download_example() -> None:
    """Download a Drive file into the user's Downloads directory."""
    result = download(
        remote_path="test.csv",
        local_path="test.csv",
        verify=True,
    )
    print(result)


def upload_example() -> None:
    """Upload a local file to the Drive root."""
    result = upload(
        local_path="test.csv",
        remote_path="test.csv",
        on_duplicate="overwrite",
        verify=True,
    )
    print(result)


if __name__ == "__main__":
    upload_example()
