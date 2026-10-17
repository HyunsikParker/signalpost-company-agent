from __future__ import annotations

from typing import Any


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
}


FIELD_LABELS = {
    "registry.name": "legal name",
    "registry.legal_form": "legal form",
    "registry.employees": "registered employees",
    "registry.municipality": "municipality",
    "registry.website": "registered website",
    "registry.latest_submitted_accounts": "latest submitted accounts",
    "financials.records": "filed accounts",
    "financial_history.years": "accounts history",
    "roles.roles": "registered roles",
    "locations.locations": "registered workplaces",
    "website.title": "website title",
    "website.description": "website description",
    "website.social_links": "website social links",
}


def change_note(changes: list[dict[str, Any]], compared: bool) -> str:
    """One summary sentence on refresh: not assessed, no material change, or the changed fields."""
    if not compared:
        return "Changes: not assessed, because no earlier run of this agent covers this company."
    if not changes:
        return "No material change since the previous run."
    parts = []
    for change in changes[:4]:
        label = FIELD_LABELS.get(change["field"], change["field"])
        old, new = change.get("old_value"), change.get("new_value")
        if isinstance(old, (list, dict)) or isinstance(new, (list, dict)):
            parts.append(f"{label} updated")
        else:
            parts.append(f"{label} {_short(old)} → {_short(new)}")
    more = f", and {len(changes) - 4} more" if len(changes) > 4 else ""
    return "Changes since the previous run: " + "; ".join(parts) + more + "."


def _short(value: Any) -> str:
    if value in (None, ""):
        return "not reported"
    text = " ".join(str(value).split())
    return text if len(text) <= 40 else text[:39] + "…"


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        old_value = _read(previous, path)
        new_value = _read(current, path)
        if old_value == new_value:
            continue
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
        })
    return changes


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]
