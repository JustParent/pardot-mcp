"""Known Account Engagement v5 objects, with safe default fields and filters.

The v5 API requires an explicit ``fields`` list on every read, so this registry
lets tools work out of the box on common objects. It is a convenience, not a
contract: objects and fields not listed here may still exist (pass explicit
``fields``), and org-specific custom prospect fields can be requested by name.
"""

from __future__ import annotations

from dataclasses import dataclass

# Filters accepted by most v5 list endpoints. Objects without soft delete or
# an updatedAt column skip the corresponding params.
COMMON_FILTERS = [
    "id",
    "idGreaterThan",
    "idLessThan",
    "createdAtAfter",
    "createdAtBefore",
    "updatedAtAfter",
    "updatedAtBefore",
]


@dataclass(frozen=True)
class ObjectSpec:
    """Default fields plus object-specific extras for one v5 object."""

    fields: tuple[str, ...]
    extra_filters: tuple[str, ...] = ()
    notes: str = ""


OBJECTS: dict[str, ObjectSpec] = {
    "prospects": ObjectSpec(
        fields=(
            "id", "email", "firstName", "lastName", "company", "jobTitle",
            "phone", "city", "state", "country", "score", "grade", "source",
            "campaignId", "userId", "prospectAccountId", "optedOut",
            "lastActivityAt", "createdAt", "updatedAt",
        ),
        extra_filters=("email", "assigned", "userId", "deleted", "lastActivityAtAfter", "lastActivityAtBefore"),
        notes="Core marketing contact. Custom fields can be requested by their field name. Supports create/update/delete.",
    ),
    "lists": ObjectSpec(
        fields=("id", "name", "title", "description", "isPublic", "isDynamic", "isDeleted", "createdAt", "updatedAt"),
        extra_filters=("deleted",),
        notes="Static and dynamic marketing lists. Supports create/update/delete (static lists).",
    ),
    "list-memberships": ObjectSpec(
        fields=("id", "listId", "prospectId", "optedOut", "createdAt", "updatedAt"),
        extra_filters=("listId", "prospectId", "deleted"),
        notes="Create one to add a prospect to a static list; delete to remove.",
    ),
    "campaigns": ObjectSpec(
        fields=("id", "name", "cost", "folderId", "salesforceId", "isDeleted", "createdAt", "updatedAt"),
        notes="Read-only when Connected Campaigns is enabled (manage them in Salesforce).",
    ),
    "custom-fields": ObjectSpec(
        fields=("id", "name", "fieldId", "type", "isRecordMultipleResponses", "isUseValues", "salesforceId", "createdAt", "updatedAt"),
        notes="Definitions of custom prospect fields; use `fieldId` values as extra `fields` on prospects queries.",
    ),
    "custom-redirects": ObjectSpec(
        fields=("id", "name", "campaignId", "folderId", "destinationUrl", "createdAt", "updatedAt"),
        notes="Tracked links.",
    ),
    "dynamic-contents": ObjectSpec(
        fields=("id", "name", "embedUrl", "createdAt", "updatedAt"),
    ),
    "emails": ObjectSpec(
        fields=("id", "name", "subject", "sentAt", "campaignId", "prospectId", "listEmailId"),
        extra_filters=("prospectId", "listEmailId", "sentAtAfter", "sentAtBefore"),
        notes="Read-only record of sent emails; high volume, filter by prospectId or sentAtAfter.",
    ),
    "email-templates": ObjectSpec(
        fields=("id", "name", "subject", "isOneToOneEmail", "isListEmail", "createdAt", "updatedAt"),
    ),
    "engagement-studio-programs": ObjectSpec(
        fields=("id", "name", "status", "createdAt", "updatedAt"),
        notes="Read-only program metadata.",
    ),
    "files": ObjectSpec(
        fields=("id", "name", "folderId", "campaignId", "url", "createdAt", "updatedAt"),
    ),
    "folders": ObjectSpec(
        fields=("id", "name", "parentFolderId", "path", "createdAt", "updatedAt"),
    ),
    "forms": ObjectSpec(
        fields=("id", "name", "campaignId", "folderId", "isDeleted", "createdAt", "updatedAt"),
        extra_filters=("deleted",),
    ),
    "form-handlers": ObjectSpec(
        fields=("id", "name", "campaignId", "folderId", "isDataForwarded", "successLocation", "errorLocation", "createdAt", "updatedAt"),
    ),
    "landing-pages": ObjectSpec(
        fields=("id", "name", "campaignId", "formId", "isDeleted", "createdAt", "updatedAt"),
        extra_filters=("deleted",),
    ),
    "layout-templates": ObjectSpec(
        fields=("id", "name", "folderId", "createdAt", "updatedAt"),
    ),
    "lifecycle-histories": ObjectSpec(
        fields=("id", "prospectId", "previousStageId", "nextStageId", "secondsElapsed", "createdAt"),
        extra_filters=("prospectId",),
        notes="Read-only; no updatedAt filters.",
    ),
    "lifecycle-stages": ObjectSpec(
        fields=("id", "name", "position", "isLocked", "createdAt", "updatedAt"),
        notes="Read-only.",
    ),
    "list-emails": ObjectSpec(
        fields=("id", "name", "subject", "campaignId", "sentAt", "createdAt", "updatedAt"),
        notes="One record per list email send.",
    ),
    "opportunities": ObjectSpec(
        fields=("id", "name", "value", "probability", "stage", "status", "type", "closedAt", "campaignId", "createdAt", "updatedAt"),
        notes="Read-only when Salesforce opportunity sync is enabled.",
    ),
    "prospect-accounts": ObjectSpec(
        fields=("id", "name", "salesforceId", "createdAt", "updatedAt"),
    ),
    "tags": ObjectSpec(
        fields=("id", "name", "createdAt", "updatedAt"),
    ),
    "tagged-objects": ObjectSpec(
        fields=("id", "tagId", "targetId", "createdAt"),
        extra_filters=("tagId",),
    ),
    "users": ObjectSpec(
        fields=("id", "email", "firstName", "lastName", "salesforceId", "isDeleted", "createdAt", "updatedAt"),
        notes="Read-only.",
    ),
    "visitors": ObjectSpec(
        fields=("id", "prospectId", "pageViewCount", "ipAddress", "hostname", "isIdentified", "doNotSell", "createdAt", "updatedAt"),
        notes="Read-only.",
    ),
    "visits": ObjectSpec(
        fields=("id", "visitorId", "prospectId", "visitorPageViewCount", "firstVisitorPageViewAt", "lastVisitorPageViewAt", "durationInSeconds", "createdAt", "updatedAt"),
        extra_filters=("visitorId", "prospectId"),
        notes="Read-only; usually filtered by visitorId or prospectId.",
    ),
    "visitor-activities": ObjectSpec(
        fields=("id", "prospectId", "visitorId", "type", "typeName", "details", "campaignId", "createdAt"),
        extra_filters=("prospectId", "visitorId"),
        notes="Read-only and very high volume — always filter (prospectId, createdAtAfter, ...).",
    ),
}


def normalize(name: str) -> str:
    """Normalize an object name to the v5 kebab-case form."""
    return name.strip().lower().replace("_", "-").replace(" ", "-")


def spec(name: str) -> ObjectSpec | None:
    return OBJECTS.get(normalize(name))


def default_fields(name: str) -> list[str] | None:
    found = spec(name)
    return list(found.fields) if found else None
