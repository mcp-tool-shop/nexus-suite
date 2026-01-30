"""
Attestation subsystem for nexus-control.

Re-exports cryptographic signing primitives from ``_signing``.
XRPL witness backend available at ``nexus_control.attestation.xrpl``.
"""

from nexus_control.attestation._signing import (
    ATTESTATION_ERROR_CLAIMS_EMPTY,
    ATTESTATION_ERROR_DIGEST_FORMAT,
    ATTESTATION_ERROR_SIGNATURE_INVALID,
    ATTESTATION_ERROR_VERSION_UNKNOWN,
    ATTESTATION_VERSION,
    VERIFY_ATTESTATION_VERSION,
    VERIFY_CLAIMS_NON_EMPTY,
    VERIFY_DIGEST_FORMAT,
    VERIFY_SIGNATURE,
    Attestation,
    AttestationPayload,
    AttestationVerificationResult,
    Attestor,
    create_attestation,
    generate_signing_key,
    get_public_key_hex,
    public_key_from_hex,
    verify_attestation_signature,
)
from nexus_control.attestation.intent import INTENT_VERSION, AttestationIntent
from nexus_control.attestation.receipt import (
    RECEIPT_VERSION,
    AttestationReceipt,
    ReceiptError,
    ReceiptErrorCode,
    ReceiptStatus,
)

__all__ = [
    "ATTESTATION_ERROR_CLAIMS_EMPTY",
    "ATTESTATION_ERROR_DIGEST_FORMAT",
    "ATTESTATION_ERROR_SIGNATURE_INVALID",
    "ATTESTATION_ERROR_VERSION_UNKNOWN",
    "ATTESTATION_VERSION",
    "INTENT_VERSION",
    "RECEIPT_VERSION",
    "VERIFY_ATTESTATION_VERSION",
    "VERIFY_CLAIMS_NON_EMPTY",
    "VERIFY_DIGEST_FORMAT",
    "VERIFY_SIGNATURE",
    "Attestation",
    "AttestationIntent",
    "AttestationPayload",
    "AttestationReceipt",
    "AttestationVerificationResult",
    "Attestor",
    "ReceiptError",
    "ReceiptErrorCode",
    "ReceiptStatus",
    "create_attestation",
    "generate_signing_key",
    "get_public_key_hex",
    "public_key_from_hex",
    "verify_attestation_signature",
]
