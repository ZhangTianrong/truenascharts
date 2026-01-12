import datetime
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import yaml

from apps_config import APPS as apps
from apps_config import CHARTS_DIR
from version_checker import DockerHubChecker, GHCRChecker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


with open(CHARTS_DIR / "catalog.json", "r", encoding="utf-8") as f:
    catalog = json.load(f)


checkers = {
    "ghcr": GHCRChecker(),
    "dockerhub": DockerHubChecker(),
}


@dataclass
class ChartVersion:
    """Represents version information for a Helm chart."""

    version: str
    app_version: str
    last_update: str
    digest: Optional[str] = None
    tag: Optional[str] = None

    @property
    def human_version(self) -> str:
        return f"{self.app_version}_{self.version}"

    def __eq__(self, other) -> bool:  # type: ignore[override]
        # Prefer digest compare when available, otherwise compare tag.
        if self.digest and other.digest:
            return self.digest == other.digest
        return self.tag == other.tag and self.app_version == other.app_version


def increment_version(version: str) -> str:
    """Increment the patch version of a semantic version."""

    major, minor, patch = version.split(".")
    patch = str(int(patch) + 1)
    return ".".join([major, minor, patch])


def _split_tag_digest(tag_value: str) -> Tuple[str, Optional[str]]:
    if "@" in tag_value:
        tag, digest = tag_value.split("@", maxsplit=1)
        return tag, digest
    return tag_value, None


def parse_version(tags, matcher: Optional[object] = None, rewriter: Optional[str] = None):
    """
    Find a tag that matches a pattern and derive an appVersion from it.

    matcher can be:
      - None
      - a regex string
      - a list/tuple of regex strings (first pattern that yields a match wins)
    """

    patterns = []
    if matcher is None:
        patterns = []
    elif isinstance(matcher, (list, tuple)):
        patterns = list(matcher)
    else:
        patterns = [matcher]

    if patterns:
        for pattern in patterns:
            for tag in tags:
                if match := re.search(pattern, tag):
                    raw_version = match[0]
                    if rewriter:
                        return tag, rewriter.format(raw_version)
                    return tag, raw_version

    # Fallback: pick a reasonable tag (avoid "latest" and avoid coarse major tags when possible).
    chosen = choose_best_tag(tags)
    app_ver = _derive_app_version_from_tag(chosen)
    if rewriter:
        app_ver = rewriter.format(app_ver)
    return chosen, app_ver


def _derive_app_version_from_tag(tag: str) -> str:
    if re.fullmatch(r"\d{10}", tag):
        return tag
    if match := re.search(r"\d{10}", tag):
        return match[0]
    if re.fullmatch(r"\d+\.\d+\.\d+", tag):
        return tag
    if match := re.search(r"\d+\.\d+\.\d+", tag):
        return match[0]
    return tag


def choose_best_tag(tags) -> str:
    """
    Heuristic tag selection.
    Prefer (in order):
      1) multi-arch timestamp tags: 2025121505
      2) semver tags: 10.10.6 (highest)
      3) arch-specific timestamp tags: 2025121505-amd64 (highest timestamp)
      4) other non-latest tags
      5) latest
    """

    tags = list(dict.fromkeys(tags))  # preserve order, de-dupe
    tags_set = set(tags)

    # 1) multi-arch timestamp tags
    ts_tags = [t for t in tags_set if re.fullmatch(r"\d{10}", t)]
    if ts_tags:
        return max(ts_tags, key=lambda t: int(t))

    # 2) semver tags
    semver_tags = [t for t in tags_set if re.fullmatch(r"\d+\.\d+\.\d+", t)]
    if semver_tags:
        def semver_key(t: str):
            a, b, c = t.split(".")
            return int(a), int(b), int(c)
        return max(semver_tags, key=semver_key)

    # 3) arch-specific timestamp tags
    arch_ts_tags = [t for t in tags_set if re.fullmatch(r"\d{10}-[A-Za-z0-9_.-]+", t)]
    if arch_ts_tags:
        def arch_ts_key(t: str):
            ts = t.split("-", 1)[0]
            return int(ts)
        return max(arch_ts_tags, key=arch_ts_key)

    # 4) any non-latest tag (keep stable-ish)
    non_latest = [t for t in tags if t != "latest"]
    if non_latest:
        # Prefer longer tags to avoid coarse majors like "10"
        return max(non_latest, key=lambda t: (len(t), t))

    # 5) give up
    return tags[0]


def check_version(app):
    app_name, app_train = app["name"], app["train"]
    logger.info(f"Checking {app_train}/{app_name}")

    local_version = ChartVersion(
        version=catalog[app_train][app_name]["latest_version"],
        app_version=catalog[app_train][app_name]["latest_app_version"],
        last_update=catalog[app_train][app_name]["last_update"],
    )

    with open(
        CHARTS_DIR / f"{app_train}/{app_name}/{local_version.version}/ix_values.yaml",
        "r",
        encoding="utf-8",
    ) as f:
        local_image_tag = yaml.safe_load(f)["image"]["tag"]
        local_version.tag, local_version.digest = _split_tag_digest(str(local_image_tag))

    checker = checkers[app["check_ver"]["type"]]
    remote_image_version = checker.get_latest_version(
        image=f"{app['check_ver']['package_owner']}/{app['check_ver']['package_name']}",
        label=app["check_ver"].get("anchor_tag", None),
    )

    remote_tags, remote_digest = remote_image_version.tags, remote_image_version.digest
    remote_tag, remote_app_version = parse_version(
        remote_tags,
        app["check_ver"].get("version_matcher", None),
        app["check_ver"].get("version_rewriter", None),
    )

    new_version = ChartVersion(
        version=increment_version(local_version.version),
        app_version=remote_app_version,
        last_update=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        digest=remote_digest,
        tag=remote_tag,
    )

    # If we have digest locally, compare digest; otherwise compare tag.
    if local_version.digest:
        need_update = (remote_digest != local_version.digest) or (remote_tag != local_version.tag)
    else:
        need_update = remote_tag != local_version.tag

    return need_update, local_version, new_version


def update_catalog(app_name: str, app_train: str, new_version: ChartVersion):
    logger.info(f"Updating catalog.json for {app_train}/{app_name}")
    catalog[app_train][app_name].update(
        {
            "latest_version": new_version.version,
            "latest_app_version": new_version.app_version,
            "latest_human_version": new_version.human_version,
            "last_update": new_version.last_update,
        }
    )
    with open(CHARTS_DIR / "catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=4, ensure_ascii=False)


def update_app_version_json(app_name: str, app_train: str, old_version: ChartVersion, new_version: ChartVersion):
    app_version_json = f"{app_train}/{app_name}/app_versions.json"
    logger.info(f"Updating {app_version_json}")

    with open(CHARTS_DIR / app_version_json, "r", encoding="utf-8") as f:
        all_versions_dict = json.load(f)

    ref_dict = all_versions_dict.get(old_version.version)
    if not ref_dict:
        raise ValueError(f"Could not find version '{old_version.version}' in {app_version_json}")

    all_versions_dict = {**{new_version.version: json.loads(json.dumps(ref_dict))}, **all_versions_dict}
    all_versions_dict[new_version.version].update(
        {
            "location": ref_dict["location"].replace(old_version.version, new_version.version),
            "version": new_version.version,
            "human_version": new_version.human_version,
            "last_update": new_version.last_update,
        }
    )
    all_versions_dict[new_version.version]["chart_metadata"].update(
        {
            "version": new_version.version,
            "appVersion": new_version.app_version,
        }
    )

    with open(CHARTS_DIR / app_version_json, "w", encoding="utf-8") as f:
        json.dump(all_versions_dict, f, indent=4, ensure_ascii=False)


def create_version_dir(app_name: str, app_train: str, old_version: ChartVersion, new_version: ChartVersion) -> str:
    old_dir = f"{app_train}/{app_name}/{old_version.version}"
    new_dir = f"{app_train}/{app_name}/{new_version.version}"
    logger.info(f"Creating new version directory at {new_dir}")

    shutil.copytree(CHARTS_DIR / old_dir, CHARTS_DIR / new_dir)

    with open(CHARTS_DIR / new_dir / "ix_values.yaml", "r", encoding="utf-8") as f:
        ix_values = yaml.safe_load(f)

    # TrueNASCharts commonly stores plain tags (no @digest). Keep it consistent.
    ix_values["image"]["tag"] = new_version.tag

    with open(CHARTS_DIR / new_dir / "Chart.yaml", "r", encoding="utf-8") as f:
        chart = yaml.safe_load(f)

    chart["version"] = new_version.version
    chart["appVersion"] = new_version.app_version

    logger.info("Writing Chart.yaml and ix_values.yaml with new version information")
    with open(CHARTS_DIR / new_dir / "ix_values.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(ix_values, f, sort_keys=False)
    with open(CHARTS_DIR / new_dir / "Chart.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(chart, f, sort_keys=False)

    # Preserve executable bit for migration scripts. On Windows, copytree won't keep it,
    # but TrueNAS requires `migrations/migrate` to be executable when app_migrations is present.
    old_migrate = f"{old_dir}/migrations/migrate"
    new_migrate = f"{new_dir}/migrations/migrate"
    try:
        old_mode = subprocess.run(
            ["git", "-C", str(CHARTS_DIR), "ls-tree", "-r", "HEAD", old_migrate],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if old_mode.startswith("100755") and (CHARTS_DIR / new_migrate).exists():
            subprocess.run(
                ["git", "-C", str(CHARTS_DIR), "update-index", "--chmod=+x", new_migrate],
                check=True,
            )
    except Exception:
        # Best-effort; if git isn't available, user can fix permissions manually later.
        pass

    return new_dir


def ensure_clean_git():
    result = subprocess.run(
        ["git", "-C", str(CHARTS_DIR), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    dirty_paths = []
    for ln in lines:
        # Porcelain format: XY <path> (or XY <old> -> <new>).
        path_part = ln[3:].strip() if len(ln) >= 4 else ln.strip()
        # Handle renames "old -> new" by checking both sides.
        paths = [p.strip() for p in path_part.split("->")]
        for p in paths:
            p_norm = p.replace("\\", "/")
            if p_norm == ".updater" or p_norm.startswith(".updater/"):
                continue
            dirty_paths.append(p)

    if dirty_paths:
        raise RuntimeError(
            "Uncommitted changes detected outside .updater/. Aborting update.\n"
            + "\n".join(dirty_paths)
        )


if __name__ == "__main__":
    for app in apps:
        app_name, app_train = app["name"], app["train"]
        need_update, old_version, new_version = check_version(app)

        if not need_update:
            logger.info(f"No update needed for {app_train}/{app_name}")
            continue

        try:
            ensure_clean_git()
        except Exception as e:
            logger.error(str(e))
            sys.exit(1)

        logger.info(f"Updating {app_train}/{app_name} from {old_version.human_version} to {new_version.human_version}")

        update_catalog(app_name, app_train, new_version)
        update_app_version_json(app_name, app_train, old_version, new_version)
        versions_dir = create_version_dir(app_name, app_train, old_version, new_version)

        # Git add + commit (optional but consistent with your existing updater)
        try:
            subprocess.run(
                ["git", "-C", str(CHARTS_DIR), "add", versions_dir, "catalog.json", f"{app_train}/{app_name}/app_versions.json"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(CHARTS_DIR), "commit", "-m", f"update {app_name} to {new_version.human_version}"],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Git operation failed: {str(e)}")
            sys.exit(1)

    logger.info("All updates completed.")


