"""Single source of truth for the app version (SemVer with a pre-release label).

Release flow:  bump __version__ here, add an entry to CHANGELOG.md, commit, then run
    .\\build.ps1 -Publish
which builds the installer and creates the GitHub release v<version> that installed copies update to.
build.ps1, callcompanion.spec and installer.iss all read the version from this file.
"""
__version__ = "1.0.0"

# GitHub repository whose Releases the auto-updater checks (owner/name).
GITHUB_REPO = "dekanyrichard87-bit/call-companion"

# 1.0.0: the Chrome extension comes from the Chrome Web Store (unlisted). The local API accepts only this id by
# default (config local_api.extension_id; set it to "" to test an unpacked copy).
EXTENSION_STORE_ID = "hmmimbnglnhilhbakdboepenlfkjgjpj"
EXTENSION_STORE_URL = "https://chromewebstore.google.com/detail/" + EXTENSION_STORE_ID


def numeric_version(v: str = __version__) -> tuple:
    """'0.5.0-beta' -> (0, 5, 0, 0): the 4-part number Windows file/installer version fields need."""
    core = v.lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = [int(p) for p in core.split(".") if p.isdigit()][:3]
    return tuple(parts + [0] * (4 - len(parts)))
