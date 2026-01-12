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
]

# Repo root (TrueNASCharts/)
CHARTS_DIR = pathlib.Path(__file__).resolve().parent.parent


