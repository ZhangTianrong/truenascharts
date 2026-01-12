import pathlib

# Apps to update. Start with a single example (community/jellyfin).
APPS = [
    {
        "name": "jellyfin",
        "train": "community",
        "check_ver": {
            "type": "dockerhub",
            "package_owner": "jellyfin",
            "package_name": "jellyfin",
            # Use Docker Hub "latest" as the anchor, but write back a stable tag.
            "anchor_tag": "latest",
            # Prefer the multi-arch timestamp tag (e.g. 2025121505).
            # Fallbacks:
            # - semver (e.g. 10.10.6)
            # - arch-specific timestamp tag if multi-arch doesn't exist (e.g. 2025121505-amd64)
            "version_matcher": [r"^\d{10}$", r"^\d+\.\d+\.\d+$", r"^\d{10}-amd64$"],
        },
    },
    {
        "name": "adguard-home",
        "train": "community",
        "check_ver": {
            "type": "dockerhub",
            "package_owner": "adguard",
            "package_name": "adguardhome",
            # Use Docker Hub "latest" as the anchor, then pick the best stable tag for that digest.
            "anchor_tag": "latest",
            # Prefer v-prefixed semver tags (e.g. v0.107.71). Chart appVersion will be derived as 0.107.71.
            "version_matcher": [r"^v\d+\.\d+\.\d+$", r"^\d+\.\d+\.\d+$"],
        },
    },
]

# Repo root (TrueNASCharts/)
CHARTS_DIR = pathlib.Path(__file__).resolve().parent.parent


