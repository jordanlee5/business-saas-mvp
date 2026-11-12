"""Read-only recovery-point/image coverage gate. Never deletes, exports or loads images."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from tools.staging_backup_job import exclusive_lock
from tools.staging_offsite_backup import RUN, is_link
from tools.staging_pg_backup import verify_backup, digest
from tools import staging_image_archive as images


def checked_path(path):
    return images.private_path(Path(path))


def root_snapshot(root, project):
    root = checked_path(root)
    for name in ("project.json", "backups", ".backup.lock"):
        checked_path(root / name)
    if not (root / ".backup.lock").is_file():
        raise ValueError("Existing backup lock required")
    if json.loads((root / "project.json").read_text()) != {"project": project}:
        raise ValueError("Wrong backup owner")
    paths = sorted((root / "backups").iterdir())
    if not paths:
        raise ValueError("Empty recovery inventory")
    result = {}
    for point in paths:
        if not RUN.fullmatch(point.name) or is_link(point) or not point.is_dir():
            raise ValueError("Unknown recovery entry")
        manifest = verify_backup(point)
        if manifest.get("source_project") != project:
            raise ValueError("Wrong recovery project")
        result[point.name] = dict(manifest_sha256=digest(point / "manifest.json"),
                                 image_id=images.image_id(manifest.get("app_image_id")),
                                 backup_format=manifest["format"])
    return result


def audit(project, roots, bundles, candidate_image=None):
    if (os.environ.get("DATABASE_URL") or not re.fullmatch(r"saas-pg-test-[a-z0-9][a-z0-9-]{0,48}", project)
            or not 1 <= len(roots) <= 16 or len(bundles) > 64):
        raise ValueError("Dedicated staging inventory required")
    if candidate_image is not None:
        images.image_id(candidate_image)
    roots, bundles = [checked_path(p) for p in roots], [checked_path(p) for p in bundles]
    if len(set(roots)) != len(roots) or len(set(bundles)) != len(bundles):
        raise ValueError("Duplicate inventory paths")
    problems, points, archives, initial = [], [], [], {}
    def problem(code, label):
        problems.append(dict(code=code, item=label))
    # Use the existing writer/retention locks only while reading recovery points.
    for index, root in enumerate(roots, 1):
        label = "source_" + str(index)
        try:
            checked_path(root / ".backup.lock")
            if not (root / ".backup.lock").is_file():
                raise ValueError("Missing lock")
            with exclusive_lock(root / ".backup.lock"):
                snapshot = root_snapshot(root, project)
            initial[label] = snapshot
            points.extend(dict(source=label, run_id=run, **item) for run,item in snapshot.items())
        except Exception:
            problem("recovery_inventory_invalid_or_busy", label)
    # A matching point copied to two hosts must describe identical bytes.
    by_run = {}
    for point in points:
        prior = by_run.setdefault(point["run_id"], point["manifest_sha256"])
        if prior != point["manifest_sha256"]:
            problem("conflicting_recovery_point", point["source"])
    for index, bundle in enumerate(bundles, 1):
        label = "archive_" + str(index)
        try:
            checked_path(bundle / "manifest.json")
            identity = images.image_id(images.json_file(bundle / "manifest.json").get("image_id"))
            proof = images.verify(bundle, identity)
            archives.append(dict(archive=label, image_id=identity, config_id=proof["config_id"],
                                 sha256=proof["sha256"], bytes=proof["bytes"], references=[],
                                 candidate_reference=False))
        except Exception:
            problem("image_archive_invalid", label)
    for point in points:
        matches = [a for a in archives if point["image_id"] in (a["image_id"], a["config_id"])]
        if not matches:
            problem("recovery_image_missing", point["source"] + "/" + point["run_id"])
        for archive in matches:
            archive["references"].append(point["source"] + "/" + point["run_id"])
    if candidate_image:
        matches = [a for a in archives if candidate_image in (a["image_id"], a["config_id"])]
        if not matches:
            problem("candidate_image_archive_missing", "candidate")
        for archive in matches:
            archive["candidate_reference"] = True
    # Expensive archive hashing ran without holding backup locks. Reject a changed
    # inventory rather than presenting a stale release/retention decision.
    for index, root in enumerate(roots, 1):
        label = "source_" + str(index)
        if label not in initial:
            continue
        try:
            with exclusive_lock(root / ".backup.lock"):
                if root_snapshot(root, project) != initial[label]:
                    raise ValueError("Inventory changed")
        except Exception:
            problem("recovery_inventory_changed_or_busy", label)
    for archive in archives:
        archive["retention"] = ("hold_incomplete_inventory" if problems else
            "required" if archive["references"] or archive["candidate_reference"] else "unreferenced_review_only")
    return dict(format=1, project=project, checked_at=datetime.now(timezone.utc).isoformat(),
                status="blocked" if problems else "ok", points=points, archives=archives, problems=problems,
                candidate_image=candidate_image, deletion_authorized=False,
                scope="Only supplied backup roots and image bundles; no deployment or complete machine recovery certification")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument("--bundle", type=Path, action="append", default=[])
    parser.add_argument("--candidate-image")
    args = parser.parse_args(argv)
    try:
        report = audit(args.project, args.root, args.bundle, args.candidate_image)
    except Exception:
        report = dict(status="blocked", problems=[dict(code="catalog_configuration_invalid")], deletion_authorized=False)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
