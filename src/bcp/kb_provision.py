"""Provision, inspect and tear down the validated knowledge base in your own AWS account.

This module is the boto3 route for building the validated-facts knowledge base. It creates, in order:

1. an S3 bucket for reviewed facts (versioned, SSE-S3 ``AES256``, public access blocked, ACLs disabled,
   a bucket policy that denies non-TLS and pre-TLS-1.2 requests, a lifecycle rule for unfinished
   multipart uploads, tagged);
2. an S3 Vectors bucket and a ``float32`` / 1024 / ``cosine`` vector index whose
   ``AMAZON_BEDROCK_TEXT`` and ``AMAZON_BEDROCK_METADATA`` keys are non-filterable (required, or
   ingestion fails on the 2 KB filterable-metadata limit);
3. an IAM service role that only ``bedrock.amazonaws.com`` can assume, scoped with
   ``aws:SourceAccount`` and ``aws:SourceArn`` (``knowledge-base/*``), with an inline policy for the
   embedding model, the facts bucket and the vector index;
4. an Amazon Bedrock knowledge base (``VECTOR``, storage ``S3_VECTORS``) tagged
   ``mcp-multirag-kb=true`` so the AWS Labs Bedrock KB retrieval MCP server lists it;
5. an S3 data source limited to the ``verified/`` prefix, chunking ``NONE``, deletion policy ``DELETE``.

Every name is derived from a config dict (see :func:`validate_config`). The account is read from STS.
Nothing is hard-coded: no account, region, owner or resource name. All three entry points are
idempotent: :func:`provision` reuses what exists, :func:`teardown` skips what is already gone, and
:func:`status` only reads.

The state JSON written by :func:`provision` keeps the keys of the original setup script
(``region``, ``account``, ``bucket``, ``bucket_arn``, ``vector_bucket``, ``vector_bucket_arn``,
``index``, ``index_arn``, ``role_arn``, ``kb_id``, ``data_source_id``, ``embedding_model``).

Run with ``bcp kb provision|status|teardown --config kb-config.json``, or without the ``bcp`` CLI::

    python -m bcp.kb_provision provision --config kb-config.json
    python -m bcp.kb_provision status --config kb-config.json
    python -m bcp.kb_provision teardown --config kb-config.json --confirm [--force]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from bcp.errors import BcpError, BcpInputError

try:  # boto3 is only needed when you actually talk to AWS; the module stays importable without it.
    import boto3
    from botocore.exceptions import ClientError, EndpointConnectionError
except ImportError:  # pragma: no cover - exercised only on installs without boto3
    boto3 = None  # type: ignore[assignment]

    class ClientError(Exception):  # type: ignore[no-redef]
        """Placeholder so ``except ClientError`` works when botocore is missing."""

        response: dict[str, Any] = {}

    class EndpointConnectionError(Exception):  # type: ignore[no-redef]
        """Placeholder so ``except EndpointConnectionError`` works when botocore is missing."""


SCHEMA_STATE = "bcp.kb-state/1"
VECTOR_DIMENSION = 1024
VECTOR_DATA_TYPE = "float32"
VECTOR_DISTANCE = "cosine"
NON_FILTERABLE_KEYS = ("AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA")
MCP_TAG_KEY = "mcp-multirag-kb"
ROLE_POLICY_NAME = "kb-access"
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
DEFAULT_INDEX_NAME = "verified-facts"
DEFAULT_DATA_SOURCE_NAME = "verified-facts"
DEFAULT_INCLUSION_PREFIX = "verified/"
DEFAULT_STATE_FILE = "kb_state.json"
DEFAULT_IAM_WAIT_SECONDS = 15
DEPLOYER_POLICY_HINT = "infra/iam/deployer-policy.json"
POLL_SECONDS = 5
KB_ACTIVE_POLLS = 60
DELETE_POLLS = 60
KB_CREATE_ATTEMPTS = 6
KB_CREATE_RETRY_SECONDS = 10
# Facts bucket lifecycle: only clean up unfinished multipart uploads. Old object versions are kept, because
# they are the restore path for a fact; add your own expiry rule if your retention policy needs one.
LIFECYCLE_CONFIGURATION: dict[str, Any] = {"Rules": [{
    "ID": "abort-incomplete-multipart-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}

PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
BUCKET_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9]))*$")
INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$")
KB_NAME_RE = re.compile(r"^(?:[0-9a-zA-Z][_-]?){1,100}$")
ROLE_RE = re.compile(r"^[\w+=,.@-]{1,64}$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.:-]*$")
ACCOUNT_RE = re.compile(r"^\d{12}$")
TAG_RE = re.compile(r"^[\w\s.:/=+@-]*$")

KNOWN_KEYS = {
    "region", "name_prefix", "kb_name", "kb_description", "embedding_model_id", "index_name",
    "data_source_name", "inclusion_prefix", "tags", "retain_data", "bucket_name", "vector_bucket_name",
    "role_name", "expected_account_id", "profile", "state_file", "iam_wait_seconds",
}
ACCESS_DENIED_CODES = {"403", "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                       "UnrecognizedClientException", "InvalidClientTokenId"}
NOT_FOUND_CODES = {"NotFoundException", "ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket",
                   "404", "NotFound"}


class KbProvisionError(BcpError):
    """A provisioning, status or teardown step failed in a way the user can act on."""


class KbConfigError(BcpInputError):
    """The knowledge-base config is missing a key or has an invalid value."""


class KbPermissionError(KbProvisionError):
    """The caller's IAM identity lacks a permission that a step needs."""


class KbModelAccessError(KbProvisionError):
    """The embedding model is not available or not enabled in the account and region."""


class KbRegionUnsupportedError(KbProvisionError):
    """The region does not offer S3 Vectors or Amazon Bedrock Knowledge Bases."""


class KbTeardownRefused(KbProvisionError):
    """Teardown would delete data that the config or flags do not allow deleting."""


@dataclass(frozen=True)
class KbConfig:
    """Validated, normalised knowledge-base config. Build it with :func:`validate_config`."""

    region: str
    name_prefix: str
    kb_name: str
    kb_description: str
    embedding_model_id: str
    index_name: str
    data_source_name: str
    inclusion_prefix: str
    tags: dict[str, str]
    retain_data: bool
    bucket_name: str | None
    vector_bucket_name: str | None
    role_name: str
    expected_account_id: str | None
    profile: str | None
    state_file: str
    iam_wait_seconds: int


@dataclass
class Names:
    """Resource names and ARNs resolved for one account and region."""

    account: str
    partition: str
    bucket: str
    vector_bucket: str
    index: str
    role: str
    kb_name: str
    data_source: str
    embedding_model_arn: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def bucket_arn(self) -> str:
        """ARN of the facts bucket."""
        return f"arn:{self.partition}:s3:::{self.bucket}"


def _fail(msg: str) -> None:
    raise KbConfigError(f"kb config: {msg}")


def _str(cfg: Mapping[str, Any], key: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = cfg.get(key)
    if value is None:  # absent or JSON null both mean "use the default"
        value = default
    if value is None:
        if required:
            _fail(f"'{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(f"'{key}' must be a non-empty string")
    return value.strip()


def validate_config(config: Mapping[str, Any]) -> KbConfig:
    """Check a raw config dict and return a :class:`KbConfig`.

    Required keys: ``region``, ``name_prefix``. Keys starting with ``_`` are ignored (use them for
    notes). Unknown keys are rejected so a typo cannot silently fall back to a default.

    Raises:
        KbConfigError: a key is missing, unknown or invalid.
    """
    if not isinstance(config, Mapping):
        _fail("must be a JSON object")
    unknown = sorted(k for k in config if not str(k).startswith("_") and k not in KNOWN_KEYS)
    if unknown:
        _fail(f"unknown key(s): {', '.join(unknown)}")
    region = _str(config, "region", required=True)
    if not REGION_RE.match(region or ""):
        _fail(f"'region' {region!r} is not an AWS Region code such as 'eu-west-1'")
    prefix = _str(config, "name_prefix", required=True) or ""
    if not (3 <= len(prefix) <= 20 and PREFIX_RE.match(prefix)):
        _fail("'name_prefix' must be 3-20 chars: lowercase letters and digits, single hyphens, "
              "starting with a letter")
    kb_name = _str(config, "kb_name", f"{prefix}-validated-kb") or ""
    if not KB_NAME_RE.match(kb_name):
        _fail("'kb_name' must be letters/digits with single '-' or '_' separators (max 100)")
    kb_description = _str(config, "kb_description", "Validated knowledge layer: reviewed facts only") or ""
    if len(kb_description) > 200:
        _fail("'kb_description' must be at most 200 characters")
    model = _str(config, "embedding_model_id", DEFAULT_EMBEDDING_MODEL) or ""
    if not MODEL_RE.match(model):
        _fail(f"'embedding_model_id' {model!r} is not a Bedrock model id")
    index = _str(config, "index_name", DEFAULT_INDEX_NAME) or ""
    if not (3 <= len(index) <= 63 and INDEX_RE.match(index)):
        _fail("'index_name' must be 3-63 chars: lowercase letters, digits, '-' and '.'")
    ds_name = _str(config, "data_source_name", DEFAULT_DATA_SOURCE_NAME) or ""
    if not KB_NAME_RE.match(ds_name):
        _fail("'data_source_name' must be letters/digits with single '-' or '_' separators")
    inclusion = _str(config, "inclusion_prefix", DEFAULT_INCLUSION_PREFIX) or ""
    if inclusion.startswith("/") or not inclusion.endswith("/") or ".." in inclusion or len(inclusion) > 300:
        _fail("'inclusion_prefix' must be a relative key prefix ending in '/', e.g. 'verified/'")
    tags = config.get("tags") or {}
    if not isinstance(tags, Mapping) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in tags.items()):
        _fail("'tags' must be an object of string keys and string values")
    if len(tags) > 40:
        _fail("'tags' may hold at most 40 entries")
    for key, value in tags.items():
        if not key or len(key) > 128 or len(value) > 256 or key.lower().startswith("aws:"):
            _fail(f"tag {key!r}: keys are 1-128 chars, values up to 256, and 'aws:' is reserved")
        if not (TAG_RE.match(key) and TAG_RE.match(value)):
            _fail(f"tag {key!r}: only letters, digits, spaces and _ . : / = + - @ are allowed")
        if key == MCP_TAG_KEY:
            _fail(f"tag '{MCP_TAG_KEY}' is set automatically on the knowledge base; remove it")
    retain = True if config.get("retain_data") is None else config.get("retain_data")
    if not isinstance(retain, bool):
        _fail("'retain_data' must be true or false")
    bucket = _str(config, "bucket_name")
    vbucket = _str(config, "vector_bucket_name")
    for key, value in (("bucket_name", bucket), ("vector_bucket_name", vbucket)):
        if value is not None and not (3 <= len(value) <= 63 and BUCKET_RE.match(value)):
            _fail(f"'{key}' must be 3-63 chars: lowercase letters, digits and single hyphens")
    role = _str(config, "role_name", f"{prefix}-kb-role-{region}") or ""
    if not ROLE_RE.match(role):
        _fail("'role_name' must be 1-64 chars of letters, digits and +=,.@_-")
    account = _str(config, "expected_account_id")
    if account is not None and not ACCOUNT_RE.match(account):
        _fail("'expected_account_id' must be a 12-digit AWS account id")
    wait = config.get("iam_wait_seconds")
    wait = DEFAULT_IAM_WAIT_SECONDS if wait is None else wait
    if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 120:
        _fail("'iam_wait_seconds' must be an integer between 0 and 120")
    return KbConfig(
        region=region or "", name_prefix=prefix, kb_name=kb_name, kb_description=kb_description,
        embedding_model_id=model, index_name=index, data_source_name=ds_name, inclusion_prefix=inclusion,
        tags=dict(tags), retain_data=retain, bucket_name=bucket, vector_bucket_name=vbucket, role_name=role,
        expected_account_id=account, profile=_str(config, "profile"),
        state_file=_str(config, "state_file", DEFAULT_STATE_FILE) or DEFAULT_STATE_FILE, iam_wait_seconds=wait,
    )


def load_config(path: str | Path) -> dict[str, Any]:
    """Read a JSON config file (UTF-8; a BOM written by Windows PowerShell is tolerated)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise KbConfigError(f"kb config: file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise KbConfigError(f"kb config: {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise KbConfigError(f"kb config: {path} must contain a JSON object")
    return data


# --------------------------------------------------------------------------------------- AWS helpers


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _message(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Message", exc))


def _is_not_found(exc: Exception) -> bool:
    return _code(exc) in NOT_FOUND_CODES


def _call(action: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Call an AWS API and turn permission and endpoint failures into clear errors."""
    try:
        return fn(**kwargs)
    except EndpointConnectionError as exc:
        raise KbRegionUnsupportedError(
            f"{action}: no endpoint in this region ({exc}). Pick a region that offers Amazon S3 Vectors "
            "and Amazon Bedrock Knowledge Bases.") from exc
    except ClientError as exc:
        if _code(exc) in ACCESS_DENIED_CODES:
            raise KbPermissionError(
                f"{action}: access denied ({_message(exc)}). Grant the caller the statements in "
                f"{DEPLOYER_POLICY_HINT}.") from exc
        raise


class _Clients:
    """Lazily created boto3 clients for one session."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._cache: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        service = {"s3v": "s3vectors", "agent": "bedrock-agent", "bedrock": "bedrock"}.get(name, name)
        if service not in self._cache:
            self._cache[service] = self._session.client(service)
        return self._cache[service]


def _session(cfg: KbConfig, session: Any | None) -> Any:
    if session is not None:
        return session
    if boto3 is None:
        raise KbProvisionError("boto3 is not installed. Install boto3 >= 1.42 in this Python environment.")
    return boto3.session.Session(region_name=cfg.region, profile_name=cfg.profile)


def resolve_names(cfg: KbConfig, clients: Any) -> Names:
    """Read the account from STS and derive every resource name and ARN.

    Raises:
        KbProvisionError: the account does not match ``expected_account_id``.
    """
    ident = _call("sts:GetCallerIdentity", clients.sts.get_caller_identity)
    account = ident["Account"]
    partition = ident.get("Arn", "arn:aws:").split(":")[1] or "aws"
    if cfg.expected_account_id and cfg.expected_account_id != account:
        raise KbProvisionError(
            f"credentials belong to account {account}, but the config expects {cfg.expected_account_id}. "
            "Switch profile or fix 'expected_account_id'.")
    bucket = cfg.bucket_name or f"{cfg.name_prefix}-kb-{account}-{cfg.region}"
    vbucket = cfg.vector_bucket_name or f"{cfg.name_prefix}-vectors-{account}-{cfg.region}"
    for label, value in (("bucket", bucket), ("vector bucket", vbucket)):
        if len(value) > 63:
            raise KbConfigError(f"kb config: derived {label} name {value!r} is longer than 63 characters; "
                                "shorten 'name_prefix' or set an explicit name")
    model_arn = f"arn:{partition}:bedrock:{cfg.region}::foundation-model/{cfg.embedding_model_id}"
    return Names(account=account, partition=partition, bucket=bucket, vector_bucket=vbucket,
                 index=cfg.index_name, role=cfg.role_name, kb_name=cfg.kb_name,
                 data_source=cfg.data_source_name, embedding_model_arn=model_arn)


def preflight(cfg: KbConfig, clients: Any) -> dict[str, Any]:
    """Read-only checks: region support for S3 Vectors and Knowledge Bases, and embedding-model access.

    Raises:
        KbRegionUnsupportedError: a service has no endpoint in the region.
        KbModelAccessError: the embedding model is not offered, not authorised or not entitled.
        KbPermissionError: the caller cannot list vector buckets or knowledge bases.
    """
    for action, fn, kwargs in (
        ("s3vectors:ListVectorBuckets", clients.s3v.list_vector_buckets, {"maxResults": 1}),
        ("bedrock:ListKnowledgeBases", clients.agent.list_knowledge_bases, {"maxResults": 1}),
    ):
        try:
            _call(action, fn, **kwargs)
        except ClientError as exc:
            if _code(exc) in {"UnknownOperationException", "InvalidAction"}:
                raise KbRegionUnsupportedError(
                    f"{action}: not available in {cfg.region}. Pick a region with Amazon S3 Vectors "
                    "and Amazon Bedrock Knowledge Bases.") from exc
            raise
    result: dict[str, Any] = {"model": cfg.embedding_model_id}
    try:
        avail = clients.bedrock.get_foundation_model_availability(modelId=cfg.embedding_model_id)
    except EndpointConnectionError as exc:
        raise KbRegionUnsupportedError(f"Amazon Bedrock has no endpoint in {cfg.region}") from exc
    except ClientError as exc:
        if _is_not_found(exc) or _code(exc) == "ValidationException":
            raise KbModelAccessError(
                f"embedding model {cfg.embedding_model_id} is not offered in {cfg.region} "
                f"({_message(exc)}). Pick another region or model.") from exc
        if _code(exc) in ACCESS_DENIED_CODES:
            # Not fatal: the create call reports model problems too. Record that we could not check.
            result["availability"] = "unchecked (no bedrock:GetFoundationModelAvailability permission)"
            return result
        raise
    problems = []
    if avail.get("regionAvailability") != "AVAILABLE":
        problems.append(f"not available in {cfg.region}")
    if avail.get("authorizationStatus") != "AUTHORIZED":
        problems.append("not authorised for this account (an IAM or SCP policy denies it)")
    if avail.get("entitlementAvailability") != "AVAILABLE":
        problems.append("no entitlement (model access not enabled)")
    agreement = (avail.get("agreementAvailability") or {}).get("status", "AVAILABLE")
    if agreement != "AVAILABLE":
        problems.append(f"model agreement status is {agreement}")
    if problems:
        raise KbModelAccessError(
            f"embedding model {cfg.embedding_model_id}: {'; '.join(problems)}. Enable access in the "
            "Amazon Bedrock console (Model access) for this region, then retry.")
    result["availability"] = "AVAILABLE"
    return result


# --------------------------------------------------------------------------------------- provision


def _tag_list(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


def bucket_policy(n: Names) -> dict[str, Any]:
    """Facts-bucket policy: deny any request that is not HTTPS, or that uses TLS older than 1.2."""
    resources = [n.bucket_arn, f"{n.bucket_arn}/*"]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": resources, "Condition": {"Bool": {"aws:SecureTransport": False}}},
        {"Sid": "DenyOutdatedTls", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": resources, "Condition": {"NumericLessThan": {"s3:TlsVersion": "1.2"}}},
    ]}


def _ensure_bucket(cfg: KbConfig, c: Any, n: Names, log: Callable[[str], None]) -> bool:
    created = False
    try:
        c.s3.head_bucket(Bucket=n.bucket, ExpectedBucketOwner=n.account)
        log(f"bucket exists: {n.bucket}")
    except ClientError as exc:
        if _code(exc) in {"403", "AccessDenied"}:
            raise KbPermissionError(
                f"bucket {n.bucket} exists but is not readable by this account (owned elsewhere or "
                f"s3:ListBucket missing). Set 'bucket_name' or see {DEPLOYER_POLICY_HINT}.") from exc
        if not _is_not_found(exc):
            raise
        kwargs: dict[str, Any] = {"Bucket": n.bucket}
        if cfg.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": cfg.region}
        try:
            _call("s3:CreateBucket", c.s3.create_bucket, **kwargs)
        except ClientError as cexc:
            if _code(cexc) == "BucketAlreadyExists":
                raise KbProvisionError(f"bucket name {n.bucket} is taken globally; set 'bucket_name'.") from cexc
            raise
        created = True
        log(f"bucket created: {n.bucket}")
    _call("s3:PutBucketPublicAccessBlock", c.s3.put_public_access_block, Bucket=n.bucket,
          PublicAccessBlockConfiguration={k: True for k in (
              "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    _call("s3:PutEncryptionConfiguration", c.s3.put_bucket_encryption, Bucket=n.bucket,
          ServerSideEncryptionConfiguration={"Rules": [
              {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    _call("s3:PutBucketVersioning", c.s3.put_bucket_versioning, Bucket=n.bucket,
          VersioningConfiguration={"Status": "Enabled"})
    _call("s3:PutBucketOwnershipControls", c.s3.put_bucket_ownership_controls, Bucket=n.bucket,
          OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    _call("s3:PutBucketPolicy", c.s3.put_bucket_policy, Bucket=n.bucket, Policy=json.dumps(bucket_policy(n)))
    _call("s3:PutLifecycleConfiguration", c.s3.put_bucket_lifecycle_configuration, Bucket=n.bucket,
          LifecycleConfiguration=LIFECYCLE_CONFIGURATION)
    if cfg.tags:
        _call("s3:PutBucketTagging", c.s3.put_bucket_tagging, Bucket=n.bucket,
              Tagging={"TagSet": _tag_list(cfg.tags)})
    return created


def _ensure_vectors(cfg: KbConfig, c: Any, n: Names, log: Callable[[str], None]) -> tuple[str, str]:
    try:
        vb = _call("s3vectors:GetVectorBucket", c.s3v.get_vector_bucket, vectorBucketName=n.vector_bucket)
        log(f"vector bucket exists: {n.vector_bucket}")
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        kwargs: dict[str, Any] = {"vectorBucketName": n.vector_bucket}
        if cfg.tags:
            kwargs["tags"] = dict(cfg.tags)
        _call("s3vectors:CreateVectorBucket", c.s3v.create_vector_bucket, **kwargs)
        log(f"vector bucket created: {n.vector_bucket}")
        vb = _call("s3vectors:GetVectorBucket", c.s3v.get_vector_bucket, vectorBucketName=n.vector_bucket)
    vb_arn = vb["vectorBucket"]["vectorBucketArn"]
    try:
        idx = _call("s3vectors:GetIndex", c.s3v.get_index, vectorBucketName=n.vector_bucket, indexName=n.index)
        log(f"index exists: {n.index}")
        _check_index(idx["index"], n)
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        kwargs = {"vectorBucketName": n.vector_bucket, "indexName": n.index, "dataType": VECTOR_DATA_TYPE,
                  "dimension": VECTOR_DIMENSION, "distanceMetric": VECTOR_DISTANCE,
                  # Bedrock writes chunk text and metadata on each vector; these must be non-filterable
                  # or ingestion hits the 2 KB filterable-metadata limit.
                  "metadataConfiguration": {"nonFilterableMetadataKeys": list(NON_FILTERABLE_KEYS)}}
        if cfg.tags:
            kwargs["tags"] = dict(cfg.tags)
        _call("s3vectors:CreateIndex", c.s3v.create_index, **kwargs)
        log(f"index created: {n.index}")
        idx = _call("s3vectors:GetIndex", c.s3v.get_index, vectorBucketName=n.vector_bucket, indexName=n.index)
    return vb_arn, idx["index"]["indexArn"]


def _check_index(index: Mapping[str, Any], n: Names) -> None:
    """Fail loudly when an existing index cannot hold Bedrock vectors (settings are immutable)."""
    keys = set((index.get("metadataConfiguration") or {}).get("nonFilterableMetadataKeys") or [])
    wrong = []
    if index.get("dimension") not in (None, VECTOR_DIMENSION):
        wrong.append(f"dimension {index.get('dimension')} != {VECTOR_DIMENSION}")
    if index.get("distanceMetric") not in (None, VECTOR_DISTANCE):
        wrong.append(f"distance {index.get('distanceMetric')} != {VECTOR_DISTANCE}")
    if not set(NON_FILTERABLE_KEYS) <= keys:
        wrong.append(f"nonFilterableMetadataKeys missing {sorted(set(NON_FILTERABLE_KEYS) - keys)}")
    if wrong:
        raise KbProvisionError(
            f"existing index {n.index} in {n.vector_bucket} cannot be reused: {'; '.join(wrong)}. "
            "Index settings cannot change; delete it (teardown) or set another 'index_name'.")


def trust_policy(n: Names, region: str) -> dict[str, Any]:
    """Trust policy: only Bedrock, only for knowledge bases in this account and region."""
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": n.account},
            "ArnLike": {"aws:SourceArn": f"arn:{n.partition}:bedrock:{region}:{n.account}:knowledge-base/*"}}}]}


def role_policy(n: Names, vb_arn: str, idx_arn: str) -> dict[str, Any]:
    """Inline service-role policy: embedding model, facts bucket (read) and vector index."""
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "InvokeEmbeddingModel", "Effect": "Allow", "Action": ["bedrock:InvokeModel"],
         "Resource": [n.embedding_model_arn]},
        {"Sid": "ListFactsBucket", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [n.bucket_arn],
         "Condition": {"StringEquals": {"aws:ResourceAccount": n.account}}},
        {"Sid": "ReadFacts", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"{n.bucket_arn}/*"],
         "Condition": {"StringEquals": {"aws:ResourceAccount": n.account}}},
        {"Sid": "VectorIndex", "Effect": "Allow", "Action": [
            "s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors",
            "s3vectors:QueryVectors", "s3vectors:GetIndex", "s3vectors:ListVectors"],
         "Resource": [vb_arn, idx_arn]},
    ]}


def _ensure_role(cfg: KbConfig, c: Any, n: Names, vb_arn: str, idx_arn: str,
                 log: Callable[[str], None], sleep: Callable[[float], None]) -> str:
    trust = json.dumps(trust_policy(n, cfg.region))
    created = False
    try:
        arn = _call("iam:GetRole", c.iam.get_role, RoleName=n.role)["Role"]["Arn"]
        _call("iam:UpdateAssumeRolePolicy", c.iam.update_assume_role_policy, RoleName=n.role,
              PolicyDocument=trust)
        log(f"role exists: {n.role}")
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        kwargs: dict[str, Any] = {"RoleName": n.role, "AssumeRolePolicyDocument": trust,
                                  "Description": "Amazon Bedrock knowledge base service role (validated facts)"}
        if cfg.tags:
            kwargs["Tags"] = _tag_list(cfg.tags)
        arn = _call("iam:CreateRole", c.iam.create_role, **kwargs)["Role"]["Arn"]
        created = True
        log(f"role created: {n.role}")
    _call("iam:PutRolePolicy", c.iam.put_role_policy, RoleName=n.role, PolicyName=ROLE_POLICY_NAME,
          PolicyDocument=json.dumps(role_policy(n, vb_arn, idx_arn)))
    if created and cfg.iam_wait_seconds:
        log(f"waiting {cfg.iam_wait_seconds} s for IAM propagation")
        sleep(cfg.iam_wait_seconds)
    return arn


def _find_kb(c: Any, name: str) -> str | None:
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = _call("bedrock:ListKnowledgeBases", c.agent.list_knowledge_bases, **kwargs)
        for kb in page.get("knowledgeBaseSummaries", []):
            if kb["name"] == name:
                return str(kb["knowledgeBaseId"])
        token = page.get("nextToken")
        if not token:
            return None


def _find_ds(c: Any, kb_id: str, name: str) -> str | None:
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"knowledgeBaseId": kb_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = _call("bedrock:ListDataSources", c.agent.list_data_sources, **kwargs)
        for ds in page.get("dataSourceSummaries", []):
            if ds["name"] == name:
                return str(ds["dataSourceId"])
        token = page.get("nextToken")
        if not token:
            return None


def _looks_like_model_error(msg: str) -> bool:
    low = msg.lower()
    return "model" in low and any(w in low for w in ("access", "not authorized", "not supported",
                                                       "subscription", "entitle", "invalid model"))


def _ensure_kb(cfg: KbConfig, c: Any, n: Names, role_arn: str, idx_arn: str,
               log: Callable[[str], None], sleep: Callable[[float], None]) -> str:
    kb_id = _find_kb(c, n.kb_name)
    if kb_id:
        log(f"KB exists: {kb_id}")
        kb = _call("bedrock:GetKnowledgeBase", c.agent.get_knowledge_base, knowledgeBaseId=kb_id)["knowledgeBase"]
        current = ((kb.get("storageConfiguration") or {}).get("s3VectorsConfiguration") or {}).get("indexArn")
        if current and current != idx_arn:
            raise KbProvisionError(
                f"knowledge base {n.kb_name} ({kb_id}) points at index {current}, not {idx_arn}. "
                "Set another 'kb_name' or tear the old one down.")
        return kb_id
    tags = {**cfg.tags, MCP_TAG_KEY: "true"}
    last: Exception | None = None
    for attempt in range(1, KB_CREATE_ATTEMPTS + 1):
        try:
            kb = _call("bedrock:CreateKnowledgeBase", c.agent.create_knowledge_base,
                       name=n.kb_name, roleArn=role_arn, description=cfg.kb_description,
                       knowledgeBaseConfiguration={"type": "VECTOR", "vectorKnowledgeBaseConfiguration": {
                           "embeddingModelArn": n.embedding_model_arn}},
                       storageConfiguration={"type": "S3_VECTORS",
                                             "s3VectorsConfiguration": {"indexArn": idx_arn}},
                       tags=tags)["knowledgeBase"]
            log(f"KB created: {kb['knowledgeBaseId']} status={kb.get('status')}")
            return str(kb["knowledgeBaseId"])
        except ClientError as exc:
            msg = _message(exc)
            if _looks_like_model_error(msg):
                raise KbModelAccessError(
                    f"knowledge base create rejected the embedding model {cfg.embedding_model_id}: {msg}. "
                    "Enable model access in the Amazon Bedrock console for this region.") from exc
            if _code(exc) not in {"ValidationException", "ConflictException", "ThrottlingException"}:
                raise
            last = exc
            log(f"create KB attempt {attempt} failed ({_code(exc)}: {msg[:200]}); retrying "
                "(IAM role propagation can take a few seconds)")
            sleep(KB_CREATE_RETRY_SECONDS)
    raise KbProvisionError(f"could not create knowledge base {n.kb_name}: {last}")


def _wait_kb_active(c: Any, kb_id: str, sleep: Callable[[float], None], log: Callable[[str], None]) -> None:
    for _ in range(KB_ACTIVE_POLLS):
        kb = _call("bedrock:GetKnowledgeBase", c.agent.get_knowledge_base, knowledgeBaseId=kb_id)["knowledgeBase"]
        status = kb.get("status")
        if status == "ACTIVE":
            log("KB ACTIVE")
            return
        if status in {"FAILED", "DELETE_UNSUCCESSFUL"}:
            raise KbProvisionError(f"knowledge base {kb_id} is {status}: {kb.get('failureReasons')}")
        sleep(POLL_SECONDS)
    raise KbProvisionError(f"knowledge base {kb_id} did not become ACTIVE in time")


def _ensure_ds(cfg: KbConfig, c: Any, n: Names, kb_id: str, log: Callable[[str], None]) -> str:
    ds_id = _find_ds(c, kb_id, n.data_source)
    if ds_id:
        log(f"data source exists: {ds_id}")
        return ds_id
    ds = _call("bedrock:CreateDataSource", c.agent.create_data_source,
               knowledgeBaseId=kb_id, name=n.data_source,
               description="Only reviewed facts are synced; stale facts are moved out of this prefix.",
               dataSourceConfiguration={"type": "S3", "s3Configuration": {
                   "bucketArn": n.bucket_arn, "inclusionPrefixes": [cfg.inclusion_prefix],
                   "bucketOwnerAccountId": n.account}},
               vectorIngestionConfiguration={"chunkingConfiguration": {"chunkingStrategy": "NONE"}},
               dataDeletionPolicy="DELETE")["dataSource"]
    log(f"data source created: {ds['dataSourceId']}")
    return str(ds["dataSourceId"])


def write_state(path: str | Path, state: Mapping[str, Any]) -> Path:
    """Write the state JSON as UTF-8 without BOM and return the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
    return target


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def provision(config: Mapping[str, Any], *, session: Any | None = None,
              sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] | None = None,
              write: bool = True) -> dict[str, Any]:
    """Create or reuse every resource and return the state dict (also written to ``state_file``).

    Safe to re-run: existing resources are reused and their settings re-applied (bucket hardening,
    trust policy, inline policy). An existing index or knowledge base with incompatible settings
    raises instead of being changed.

    Raises:
        KbConfigError, KbPermissionError, KbModelAccessError, KbRegionUnsupportedError, KbProvisionError.
    """
    cfg = validate_config(config)
    say = log or _print
    c = _Clients(_session(cfg, session))
    n = resolve_names(cfg, c)
    say(f"account {n.account}, region {cfg.region}")
    check = preflight(cfg, c)
    _ensure_bucket(cfg, c, n, say)
    vb_arn, idx_arn = _ensure_vectors(cfg, c, n, say)
    role_arn = _ensure_role(cfg, c, n, vb_arn, idx_arn, say, sleep)
    kb_id = _ensure_kb(cfg, c, n, role_arn, idx_arn, say, sleep)
    _wait_kb_active(c, kb_id, sleep, say)
    ds_id = _ensure_ds(cfg, c, n, kb_id, say)
    state = {
        "schema": SCHEMA_STATE, "region": cfg.region, "account": n.account, "name_prefix": cfg.name_prefix,
        "bucket": n.bucket, "bucket_arn": n.bucket_arn, "vector_bucket": n.vector_bucket,
        "vector_bucket_arn": vb_arn, "index": n.index, "index_arn": idx_arn, "role_name": n.role,
        "role_arn": role_arn, "kb_name": n.kb_name, "kb_id": kb_id, "data_source_name": n.data_source,
        "data_source_id": ds_id, "inclusion_prefix": cfg.inclusion_prefix,
        "embedding_model": n.embedding_model_arn, "model_check": check.get("availability"),
        "retain_data": cfg.retain_data, "provisioned_at": _now(),
    }
    if write:
        say(f"state written: {write_state(cfg.state_file, state)}")
    return state


# --------------------------------------------------------------------------------------- status


def status(config: Mapping[str, Any], *, session: Any | None = None) -> dict[str, Any]:
    """Read-only report of every resource: whether it exists, its id/ARN and the KB and last sync status."""
    cfg = validate_config(config)
    c = _Clients(_session(cfg, session))
    return _status(cfg, c, resolve_names(cfg, c))


def _status(cfg: KbConfig, c: Any, n: Names) -> dict[str, Any]:
    out: dict[str, Any] = {"region": cfg.region, "account": n.account, "resources": {}}
    res = out["resources"]
    try:
        _call("s3:HeadBucket", c.s3.head_bucket, Bucket=n.bucket, ExpectedBucketOwner=n.account)
        res["bucket"] = {"name": n.bucket, "exists": True}
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        res["bucket"] = {"name": n.bucket, "exists": False}
    try:
        vb = _call("s3vectors:GetVectorBucket", c.s3v.get_vector_bucket, vectorBucketName=n.vector_bucket)
        res["vector_bucket"] = {"name": n.vector_bucket, "exists": True,
                                "arn": vb["vectorBucket"]["vectorBucketArn"]}
        try:
            idx = _call("s3vectors:GetIndex", c.s3v.get_index, vectorBucketName=n.vector_bucket,
                        indexName=n.index)
            res["index"] = {"name": n.index, "exists": True, "arn": idx["index"]["indexArn"]}
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
            res["index"] = {"name": n.index, "exists": False}
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        res["vector_bucket"] = {"name": n.vector_bucket, "exists": False}
        res["index"] = {"name": n.index, "exists": False}
    try:
        role = _call("iam:GetRole", c.iam.get_role, RoleName=n.role)
        res["role"] = {"name": n.role, "exists": True, "arn": role["Role"]["Arn"]}
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        res["role"] = {"name": n.role, "exists": False}
    kb_id = _find_kb(c, n.kb_name)
    res["knowledge_base"] = {"name": n.kb_name, "exists": bool(kb_id)}
    res["data_source"] = {"name": n.data_source, "exists": False}
    if kb_id:
        kb = _call("bedrock:GetKnowledgeBase", c.agent.get_knowledge_base, knowledgeBaseId=kb_id)["knowledgeBase"]
        res["knowledge_base"].update({"id": kb_id, "status": kb.get("status")})
        ds_id = _find_ds(c, kb_id, n.data_source)
        if ds_id:
            res["data_source"].update({"exists": True, "id": ds_id})
            jobs = _call("bedrock:ListIngestionJobs", c.agent.list_ingestion_jobs, knowledgeBaseId=kb_id,
                         dataSourceId=ds_id, maxResults=1,
                         sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"})
            summaries = jobs.get("ingestionJobSummaries", [])
            if summaries:
                job = summaries[0]
                res["data_source"]["last_ingestion"] = {
                    "id": job.get("ingestionJobId"), "status": job.get("status"),
                    "started_at": job.get("startedAt"), "statistics": job.get("statistics")}
    out["ready"] = (all(r.get("exists") for r in res.values())
                    and res["knowledge_base"].get("status") == "ACTIVE")
    return out


# --------------------------------------------------------------------------------------- teardown


def _bucket_is_empty(c: Any, bucket: str) -> bool:
    page = _call("s3:ListObjectVersions", c.s3.list_object_versions, Bucket=bucket, MaxKeys=1)
    return not (page.get("Versions") or page.get("DeleteMarkers"))


def _empty_bucket(c: Any, bucket: str) -> int:
    """Delete every object version and delete marker. Returns how many were deleted."""
    deleted = 0
    kwargs: dict[str, Any] = {"Bucket": bucket}
    while True:
        page = _call("s3:ListObjectVersions", c.s3.list_object_versions, **kwargs)
        keys = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])]
        for i in range(0, len(keys), 1000):
            _call("s3:DeleteObjectVersion", c.s3.delete_objects, Bucket=bucket,
                  Delete={"Objects": keys[i:i + 1000], "Quiet": True})
        deleted += len(keys)
        if not page.get("IsTruncated"):
            return deleted
        kwargs = {"Bucket": bucket, "KeyMarker": page.get("NextKeyMarker"),
                  "VersionIdMarker": page.get("NextVersionIdMarker")}


def _wait_gone(fn: Callable[..., Any], sleep: Callable[[float], None], what: str, **kwargs: Any) -> None:
    for _ in range(DELETE_POLLS):
        try:
            resp = fn(**kwargs)
        except ClientError as exc:
            if _is_not_found(exc):
                return
            raise
        body = resp.get("dataSource") or resp.get("knowledgeBase") or {}
        if body.get("status") == "DELETE_UNSUCCESSFUL":
            raise KbProvisionError(f"{what} could not be deleted: {body.get('failureReasons')}")
        sleep(POLL_SECONDS)
    raise KbProvisionError(f"{what} is still being deleted; re-run teardown in a few minutes")


def teardown(config: Mapping[str, Any], confirm: bool = False, *, force: bool = False,
             session: Any | None = None, sleep: Callable[[float], None] = time.sleep,
             log: Callable[[str], None] | None = None, write: bool = True) -> dict[str, Any]:
    """Delete the resources in dependency order; without ``confirm`` only report the plan.

    Order: data source, knowledge base, vector index, vector bucket, role inline policy, role, and
    last the facts bucket. The facts bucket is deleted only when ``retain_data`` is false in the
    config **and** ``confirm`` is true. A non-empty facts bucket is refused (before anything is
    deleted) unless ``force`` is true, in which case every object version is deleted first.

    Returns:
        ``{"dry_run": bool, "plan": [...], "deleted": [...], "retained": [...]}``.

    Raises:
        KbTeardownRefused: the facts bucket holds data and ``force`` is false.
    """
    cfg = validate_config(config)
    say = log or _print
    c = _Clients(_session(cfg, session))
    n = resolve_names(cfg, c)
    current = _status(cfg, c, n)
    res = current["resources"]
    kb_id = res["knowledge_base"].get("id")
    ds_id = res["data_source"].get("id")
    plan: list[str] = []
    if ds_id:
        plan.append(f"data source {n.data_source} ({ds_id})")
    if kb_id:
        plan.append(f"knowledge base {n.kb_name} ({kb_id})")
    if res["index"]["exists"]:
        plan.append(f"vector index {n.index}")
    if res["vector_bucket"]["exists"]:
        plan.append(f"vector bucket {n.vector_bucket}")
    if res["role"]["exists"]:
        plan.append(f"role policy {ROLE_POLICY_NAME} and role {n.role}")
    retained: list[str] = []
    delete_bucket = res["bucket"]["exists"] and not cfg.retain_data
    if res["bucket"]["exists"] and cfg.retain_data:
        retained.append(f"bucket {n.bucket} (retain_data is true)")
    if delete_bucket:
        plan.append(f"bucket {n.bucket}")
    result: dict[str, Any] = {"dry_run": not confirm, "plan": plan, "deleted": [], "retained": retained}
    if not confirm:
        say("dry run (pass confirm=True / --confirm to delete): " + ("; ".join(plan) or "nothing to delete"))
        return result
    if delete_bucket and not force and not _bucket_is_empty(c, n.bucket):
        raise KbTeardownRefused(
            f"bucket {n.bucket} is not empty; nothing was deleted. Back it up and re-run with force=True "
            "(--force), or set retain_data to true to keep it.")
    done = result["deleted"]
    if ds_id and kb_id:
        try:
            _call("bedrock:DeleteDataSource", c.agent.delete_data_source, knowledgeBaseId=kb_id, dataSourceId=ds_id)
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        _wait_gone(c.agent.get_data_source, sleep, f"data source {ds_id}", knowledgeBaseId=kb_id,
                   dataSourceId=ds_id)
        done.append(f"data source {ds_id}")
        say(f"deleted data source {ds_id}")
    if kb_id:
        try:
            _call("bedrock:DeleteKnowledgeBase", c.agent.delete_knowledge_base, knowledgeBaseId=kb_id)
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        _wait_gone(c.agent.get_knowledge_base, sleep, f"knowledge base {kb_id}", knowledgeBaseId=kb_id)
        done.append(f"knowledge base {kb_id}")
        say(f"deleted knowledge base {kb_id}")
    for exists, action, fn, kwargs, label in (
        (res["index"]["exists"], "s3vectors:DeleteIndex", c.s3v.delete_index,
         {"vectorBucketName": n.vector_bucket, "indexName": n.index}, f"vector index {n.index}"),
        (res["vector_bucket"]["exists"], "s3vectors:DeleteVectorBucket", c.s3v.delete_vector_bucket,
         {"vectorBucketName": n.vector_bucket}, f"vector bucket {n.vector_bucket}"),
        (res["role"]["exists"], "iam:DeleteRolePolicy", c.iam.delete_role_policy,
         {"RoleName": n.role, "PolicyName": ROLE_POLICY_NAME}, f"role policy {ROLE_POLICY_NAME}"),
        (res["role"]["exists"], "iam:DeleteRole", c.iam.delete_role, {"RoleName": n.role}, f"role {n.role}"),
    ):
        if not exists:
            continue
        try:
            _call(action, fn, **kwargs)
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        done.append(label)
        say(f"deleted {label}")
    if delete_bucket:
        if force:
            say(f"emptied {n.bucket}: {_empty_bucket(c, n.bucket)} object versions")
        _call("s3:DeleteBucket", c.s3.delete_bucket, Bucket=n.bucket, ExpectedBucketOwner=n.account)
        done.append(f"bucket {n.bucket}")
        say(f"deleted bucket {n.bucket}")
    if write:
        state = {"schema": SCHEMA_STATE, "region": cfg.region, "account": n.account,
                 "name_prefix": cfg.name_prefix, "torn_down_at": _now(), "deleted": done,
                 "retained": retained}
        if not delete_bucket and res["bucket"]["exists"]:
            state.update({"bucket": n.bucket, "bucket_arn": n.bucket_arn})
        write_state(cfg.state_file, state)
    return result


# --------------------------------------------------------------------------------------- command line


def _print(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    """Minimal command line without the ``bcp`` CLI (same as ``bcp kb provision|status|teardown``)."""
    parser = argparse.ArgumentParser(prog="python -m bcp.kb_provision", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("provision", "status", "teardown"))
    parser.add_argument("--config", required=True, help="path to the kb config JSON")
    parser.add_argument("--confirm", action="store_true", help="teardown: actually delete")
    parser.add_argument("--force", action="store_true", help="teardown: empty a non-empty facts bucket")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "provision":
            result = provision(cfg)
        elif args.command == "status":
            result = status(cfg)
        else:
            result = teardown(cfg, confirm=args.confirm, force=args.force)
    except BcpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
