from app.domain.fundamentals.value_objects import CANONICAL_ACCOUNT_FIELDS

ACCOUNT_MAPPING_STATUS = {
    field: "requires_official_statement_mapping_verification" for field in CANONICAL_ACCOUNT_FIELDS
}

