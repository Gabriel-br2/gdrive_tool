"""Manual examples for uploading and downloading files."""

from pathlib import Path

from gdrive_tool import download, upload


def download_example() -> None:
    """Download a Drive file into the user's Downloads directory."""
    result = download(
        "Projects/mainUse.ipynb",
        Path.home() / "Downloads" / "mainUse.ipynb",
        verify=True,
    )
    print(result)


def upload_example() -> None:
    """Upload a local file to the Drive's Projects directory."""
    result = upload(
        Path.home() / "Documents" / "report.txt",
        "Projects/report.txt",
        verify=True,
    )
    print(result)


if __name__ == "__main__":
    download_example()
