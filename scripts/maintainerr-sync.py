#!/usr/bin/env python3
"""Write config/maintainerr/rules.toml to Maintainerr: rule groups and job schedules.

Runs once on each deploy (compose service maintainerr-sync). The file is the source
of truth; a change made in the Maintainerr UI is overwritten.

For each [[rule_group]] in the file, matched by name:
- The rules (Maintainerr rule YAML) are decoded by Maintainerr itself
  (POST /api/rules/yaml/decode), so the file uses Maintainerr's own field names.
- The last condition of each rule section must be "AND <Sonarr|Radarr>.tags
  NOT_CONTAINS keep". Maintainerr filters a section condition by condition and then
  joins sections as a union or an intersection, so with that condition no section,
  and no join of sections, can hold an item tagged keep. A group without it stops
  the sync before anything is written.
- Managed settings (active, arr_action, delete_after_days, list_exclusions,
  *_settings_id) come from the file. All other settings keep their live values.
- The group is written (PUT /api/rules) only if it differs, then read back and
  compared again.

The sync stops with exit 1, before any write, if: a group in the file is missing in
Maintainerr, Maintainerr has a group that is not in the file, or library_id or
data_type differ (Maintainerr empties the collection and its exclusions when they
change). It does not create or delete groups.

[settings] rules_handler_job_cron and collection_handler_job_cron are written with
PATCH /api/settings if they differ.

Env: MAINTAINERR_API_KEY (required), MAINTAINERR_URL (default
http://maintainerr:6246/api), RULES_FILE (default /config/rules.toml),
DRY_RUN=1 (report differences, write nothing), WAIT_SECONDS (default 300).
"""
import difflib
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request

URL = os.environ.get("MAINTAINERR_URL", "http://maintainerr:6246/api").rstrip("/")
KEY = os.environ["MAINTAINERR_API_KEY"]
RULES_FILE = os.environ.get("RULES_FILE", "/config/rules.toml")
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "300"))

# Maintainerr enums (packages/contracts: ServarrAction, RulePossibility, RuleOperators).
ARR_ACTIONS = [
    "DELETE",
    "UNMONITOR_DELETE_ALL",
    "UNMONITOR_DELETE_EXISTING",
    "UNMONITOR",
    "DO_NOTHING",
    "DELETE_SHOW_IF_EMPTY",
    "UNMONITOR_SHOW_IF_EMPTY",
    "CHANGE_QUALITY_PROFILE",
]
NOT_CONTAINS = 9
AND = 0
TEXT = 2
KEEP_TAG = "keep"
ARR_APP = {"movie": "Radarr", "show": "Sonarr"}

# Fields that the file manages: file key -> (top-level API field, collection field).
MANAGED = {
    "active": ("isActive", "isActive"),
    "arr_action": ("arrAction", "arrAction"),
    "delete_after_days": (None, "deleteAfterDays"),
    "list_exclusions": ("listExclusions", "listExclusions"),
    "radarr_settings_id": ("radarrSettingsId", "radarrSettingsId"),
    "sonarr_settings_id": ("sonarrSettingsId", "sonarrSettingsId"),
}
# Collection settings that the update API reads from the top level of the body.
TOP_LEVEL_FROM_COLLECTION = [
    "arrAction", "listExclusions", "cleanupLeftoverFolders", "forceSeerr",
    "tautulliWatchedPercentOverride", "radarrSettingsId", "sonarrSettingsId",
    "sportarrSettingsId", "radarrQualityProfileId", "sonarrQualityProfileId",
    "sportarrQualityProfileId", "tagInArr", "keepInMaintainerrOnly",
]


def log(msg):
    print(f"maintainerr-sync: {msg}", flush=True)


def api(method, path, body=None):
    req = urllib.request.Request(
        URL + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Api-Key": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: HTTP {e.code}: {e.read()[:500]!r}") from None
    return json.loads(raw) if raw else None


def ok(result, what):
    if not isinstance(result, dict) or result.get("code") != 1:
        raise RuntimeError(f"{what} failed: {json.dumps(result)[:500]}")
    return result


def wait_for_api():
    deadline = time.time() + WAIT_SECONDS
    while True:
        try:
            api("GET", "/settings")
            return
        except Exception as e:  # noqa: BLE001 - any failure means "not up yet"
            if time.time() > deadline:
                raise RuntimeError(f"Maintainerr API not reachable after {WAIT_SECONDS}s: {e}")
            time.sleep(5)


def norm_rule(r):
    cv = r.get("customVal")
    op = r.get("operator")
    return {
        "operator": None if op in (None, "") else int(op),
        "action": int(r["action"]),
        "section": int(r.get("section") or 0),
        "firstVal": [int(x) for x in r["firstVal"]],
        "lastVal": [int(x) for x in r["lastVal"]] if r.get("lastVal") else None,
        "customVal": {"ruleTypeId": int(cv["ruleTypeId"]), "value": str(cv["value"])} if cv else None,
        "arrDiskPath": r.get("arrDiskPath") or None,
        "username": r.get("username") or None,
    }


def live_rules(group):
    rows = sorted(group.get("rules") or [], key=lambda r: r["id"])
    return [json.loads(r["ruleJson"]) for r in rows]


def tags_ref(constants, app_name):
    """[application id, property id] of the tags list of Sonarr or Radarr."""
    for app in constants["applications"]:
        if app["name"].lower() == app_name.lower():
            for prop in app["props"]:
                if prop["name"] == "tags":
                    return [int(app["id"]), int(prop["id"])]
    raise RuntimeError(f"Maintainerr has no {app_name} tags rule field")


def check_keep(name, rules, ref):
    """Each section must end with AND <arr>.tags NOT_CONTAINS keep."""
    sections = {}
    for i, r in enumerate(rules):
        sections.setdefault(r["section"], []).append((i, r))
    if not sections:
        raise RuntimeError(f"'{name}': no rules")
    for section, items in sections.items():
        _, last = items[-1]
        is_keep = (
            last["firstVal"] == ref
            and last["action"] == NOT_CONTAINS
            and last["customVal"] == {"ruleTypeId": TEXT, "value": KEEP_TAG}
        )
        # A section's first condition carries the operator that joins it to the
        # previous section, so it may be anything there.
        joins_with_and = len(items) == 1 or last["operator"] == AND
        if not (is_keep and joins_with_and):
            raise RuntimeError(
                f"'{name}' section {section}: the last condition must be "
                f"AND tags NOT_CONTAINS {KEEP_TAG} (keep tag protection)"
            )


def build_body(live, spec, rules):
    coll = dict(live["collection"])
    body = {
        "id": live["id"],
        "name": live["name"],
        "description": live.get("description") or "",
        "libraryId": live["libraryId"],
        "dataType": live["dataType"],
        "isActive": live["isActive"],
        "useRules": True,
        "ruleHandlerCronSchedule": live.get("ruleHandlerCronSchedule"),
        "notifications": live.get("notifications") or [],
    }
    for field in TOP_LEVEL_FROM_COLLECTION:
        if coll.get(field) is not None:
            body[field] = coll[field]
    for key, (top, col) in MANAGED.items():
        if key in spec:
            value = spec[key]
        elif key.endswith("_settings_id"):
            value = None
        else:
            continue
        if key == "arr_action":
            value = ARR_ACTIONS.index(value)
        if top:
            body[top] = value
            if value is None:
                body.pop(top)
        if col:
            coll[col] = value
    body["collection"] = coll
    body["rules"] = rules
    return body


def managed_view(group):
    """The managed values of a live group, in file terms."""
    coll = group["collection"]
    view = {
        "active": group["isActive"],
        "arr_action": ARR_ACTIONS[coll["arrAction"]] if coll.get("arrAction") is not None else None,
        "delete_after_days": coll.get("deleteAfterDays"),
        "list_exclusions": coll.get("listExclusions"),
        "radarr_settings_id": coll.get("radarrSettingsId"),
        "sonarr_settings_id": coll.get("sonarrSettingsId"),
    }
    return view


def wanted_view(spec):
    return {
        "active": spec.get("active", True),
        "arr_action": spec["arr_action"],
        "delete_after_days": spec.get("delete_after_days"),
        "list_exclusions": spec.get("list_exclusions", False),
        "radarr_settings_id": spec.get("radarr_settings_id"),
        "sonarr_settings_id": spec.get("sonarr_settings_id"),
    }


def yaml_of(rules, data_type):
    res = ok(api("POST", "/rules/yaml/encode", {"rules": json.dumps(rules), "mediaType": data_type}), "yaml encode")
    return res["result"]


def main():
    with open(RULES_FILE, "rb") as f:
        cfg = tomllib.load(f)
    specs = cfg.get("rule_group", [])
    names = [s["name"] for s in specs]
    if len(set(names)) != len(names):
        raise RuntimeError("duplicate rule_group names in the file")

    wait_for_api()
    constants = api("GET", "/rules/constants")
    live = {g["name"]: g for g in api("GET", "/rules")}

    # Validate everything before the first write.
    unknown = sorted(set(live) - set(names))
    if unknown:
        raise RuntimeError(f"Maintainerr has rule groups that are not in {RULES_FILE}: {unknown}")
    plan = []
    for spec in specs:
        name = spec["name"]
        if name not in live:
            raise RuntimeError(f"rule group '{name}' does not exist in Maintainerr (the sync does not create groups)")
        group = live[name]
        if spec["data_type"] != group["dataType"] or spec["library_id"] != group["libraryId"]:
            raise RuntimeError(
                f"'{name}': library_id or data_type differ from Maintainerr; "
                "changing them empties the collection, so the sync does not do it"
            )
        if spec["arr_action"] not in ARR_ACTIONS:
            raise RuntimeError(f"'{name}': unknown arr_action {spec['arr_action']}")
        decoded = ok(
            api("POST", "/rules/yaml/decode", {"yaml": spec["rules"], "mediaType": spec["data_type"]}),
            f"'{name}': yaml decode",
        )["result"]
        decoded = json.loads(decoded) if isinstance(decoded, str) else decoded
        rules = decoded["rules"]
        check_keep(name, [norm_rule(r) for r in rules], tags_ref(constants, ARR_APP[spec["data_type"]]))
        plan.append((spec, group, rules))

    changed = 0
    for spec, group, rules in plan:
        name = spec["name"]
        current = live_rules(group)
        rules_same = [norm_rule(r) for r in current] == [norm_rule(r) for r in rules]
        have, want = managed_view(group), wanted_view(spec)
        diffs = [f"{k}: {have[k]!r} -> {want[k]!r}" for k in want if have[k] != want[k]]
        if rules_same and not diffs:
            log(f"'{name}': unchanged")
            continue
        changed += 1
        for d in diffs:
            log(f"'{name}': {d}")
        if not rules_same:
            diff = difflib.unified_diff(
                yaml_of(current, group["dataType"]).splitlines(),
                yaml_of(rules, group["dataType"]).splitlines(),
                "maintainerr", RULES_FILE, lineterm="",
            )
            for line in diff:
                log(f"'{name}': {line}")
        if DRY_RUN:
            log(f"'{name}': DRY_RUN, not written")
            continue
        ok(api("PUT", "/rules", build_body(group, spec, rules)), f"'{name}': update")
        after = api("GET", f"/rules/{group['id']}")
        if [norm_rule(r) for r in live_rules(after)] != [norm_rule(r) for r in rules] or managed_view(after) != want:
            raise RuntimeError(f"'{name}': read-back after the update does not match the file")
        log(f"'{name}': updated")

    settings = cfg.get("settings", {})
    live_settings = api("GET", "/settings")
    patch = {k: v for k, v in settings.items() if live_settings.get(k) != v}
    for k, v in patch.items():
        log(f"settings {k}: {live_settings.get(k)!r} -> {v!r}")
    if patch and not DRY_RUN:
        res = api("PATCH", "/settings", patch)
        if not isinstance(res, dict) or res.get("code") != 1:
            raise RuntimeError(f"settings update failed: {json.dumps(res)[:500]}")
        live_settings = api("GET", "/settings")
        if any(live_settings.get(k) != v for k, v in patch.items()):
            raise RuntimeError("settings read-back does not match the file")
    changed += len(patch)

    log(f"done: {changed} change(s){' (DRY_RUN)' if DRY_RUN else ''}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        sys.exit(1)
