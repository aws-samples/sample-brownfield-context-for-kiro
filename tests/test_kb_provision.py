"""Tests for bcp.kb_provision: provision, idempotent re-run, teardown order and refusals, config checks.

All AWS calls go to real boto3 clients wrapped in botocore Stubbers, so request parameters are
validated against the service models and nothing leaves the machine.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# boto3/botocore are pinned in requirements-lock.txt; skip (not a collection error) where they are absent.
boto3 = pytest.importorskip("boto3")
pytest.importorskip("botocore")

from botocore.exceptions import EndpointConnectionError  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

from bcp import kb_provision as kp  # noqa: E402

ACCOUNT = "111122223333"
REGION = "eu-west-1"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PREFIX = "acme-kb"
BUCKET = f"{PREFIX}-kb-{ACCOUNT}-{REGION}"
VBUCKET = f"{PREFIX}-vectors-{ACCOUNT}-{REGION}"
VB_ARN = f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/{VBUCKET}"
IDX_ARN = f"{VB_ARN}/index/verified-facts"
ROLE = f"{PREFIX}-kb-role-{REGION}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"
KB_NAME = f"{PREFIX}-validated-kb"
KB_ID = "KB12345678"
DS_ID = "DS12345678"
MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"
TAGS = {"project": "validated-kb", "owner": "platform-team"}
STATE_KEYS = {"region", "account", "bucket", "bucket_arn", "vector_bucket", "vector_bucket_arn", "index",
              "index_arn", "role_arn", "kb_id", "data_source_id", "embedding_model"}


def names() -> Any:
    """Derived names for the eu-west-1 test config."""
    return kp.Names(account=ACCOUNT, partition="aws", bucket=BUCKET, vector_bucket=VBUCKET,
                    index="verified-facts", role=ROLE, kb_name=KB_NAME, data_source="verified-facts",
                    embedding_model_arn=MODEL_ARN)



_FAKE_CREDENTIAL = "testing"  # botocore Stubber needs some credentials; never a real key

class StubSession:
    """Session double: hands out stubbed real clients and records every call across services."""

    def __init__(self, region: str = REGION) -> None:
        self._session = boto3.session.Session(region_name=region, aws_access_key_id=_FAKE_CREDENTIAL,
                                              aws_secret_access_key=_FAKE_CREDENTIAL,
                                              aws_session_token=_FAKE_CREDENTIAL)
        self.clients: dict[str, Any] = {}
        self.stubs: dict[str, Stubber] = {}
        self.calls: list[str] = []
        self.params: dict[str, dict[str, Any]] = {}

    def _record(self, service: str, model: Any, params: dict[str, Any], **_: Any) -> None:
        self.calls.append(f"{service}.{model.name}")
        self.params[f"{service}.{model.name}"] = dict(params)

    def client(self, service: str) -> Any:
        if service not in self.clients:
            client = self._session.client(service)
            client.meta.events.register("before-parameter-build", partial(self._record, service))
            stub = Stubber(client)
            stub.activate()
            self.clients[service], self.stubs[service] = client, stub
        return self.clients[service]

    def stub(self, service: str) -> Stubber:
        self.client(service)
        return self.stubs[service]

    def assert_done(self) -> None:
        for stub in self.stubs.values():
            stub.assert_no_pending_responses()


def base_config(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    cfg = {"region": REGION, "name_prefix": PREFIX, "tags": dict(TAGS), "retain_data": True,
           "state_file": str(tmp_path / "out" / "kb_state.json"), "_note": "ignored"}
    cfg.update(extra)
    return cfg


# ------------------------------------------------------------------------------ response builders


def sts(s: StubSession) -> None:
    s.stub("sts").add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/deployer", "UserId": "AIDEXAMPLE"}, {})


def model_ok(s: StubSession, **override: Any) -> None:
    body = {"modelId": "amazon.titan-embed-text-v2", "agreementAvailability": {"status": "AVAILABLE"},
            "authorizationStatus": "AUTHORIZED", "entitlementAvailability": "AVAILABLE",
            "regionAvailability": "AVAILABLE"}
    body.update(override)
    s.stub("bedrock").add_response("get_foundation_model_availability", body,
                                   {"modelId": "amazon.titan-embed-text-v2:0"})


def preflight_ok(s: StubSession) -> None:
    s.stub("s3vectors").add_response("list_vector_buckets", {"vectorBuckets": []}, {"maxResults": 1})
    s.stub("bedrock-agent").add_response("list_knowledge_bases", {"knowledgeBaseSummaries": []},
                                         {"maxResults": 1})
    model_ok(s)


def vector_bucket(s: StubSession) -> None:
    s.stub("s3vectors").add_response("get_vector_bucket", {"vectorBucket": {
        "vectorBucketName": VBUCKET, "vectorBucketArn": VB_ARN, "creationTime": NOW}},
        {"vectorBucketName": VBUCKET})


def index(s: StubSession, keys: tuple[str, ...] = kp.NON_FILTERABLE_KEYS, dimension: int = 1024) -> None:
    s.stub("s3vectors").add_response("get_index", {"index": {
        "vectorBucketName": VBUCKET, "indexName": "verified-facts", "indexArn": IDX_ARN, "creationTime": NOW,
        "dataType": "float32", "dimension": dimension, "distanceMetric": "cosine",
        "metadataConfiguration": {"nonFilterableMetadataKeys": list(keys)}}},
        {"vectorBucketName": VBUCKET, "indexName": "verified-facts"})


def role() -> dict[str, Any]:
    return {"Role": {"Path": "/", "RoleName": ROLE, "RoleId": "AROAEXAMPLEEXAMPLE1", "Arn": ROLE_ARN,
                     "CreateDate": NOW}}


def kb_body(status: str = "ACTIVE") -> dict[str, Any]:
    return {"knowledgeBase": {
        "knowledgeBaseId": KB_ID, "name": KB_NAME, "roleArn": ROLE_ARN, "status": status,
        "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/{KB_ID}",
        "knowledgeBaseConfiguration": {"type": "VECTOR",
                                       "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": MODEL_ARN}},
        "storageConfiguration": {"type": "S3_VECTORS", "s3VectorsConfiguration": {"indexArn": IDX_ARN}},
        "createdAt": NOW, "updatedAt": NOW}}


def kb_listed(s: StubSession) -> None:
    s.stub("bedrock-agent").add_response("list_knowledge_bases", {"knowledgeBaseSummaries": [
        {"knowledgeBaseId": "OTHER00001", "name": "someone-else", "status": "ACTIVE", "updatedAt": NOW},
        {"knowledgeBaseId": KB_ID, "name": KB_NAME, "status": "ACTIVE", "updatedAt": NOW}]},
        {"maxResults": 100})


def ds_listed(s: StubSession) -> None:
    s.stub("bedrock-agent").add_response("list_data_sources", {"dataSourceSummaries": [
        {"knowledgeBaseId": KB_ID, "dataSourceId": DS_ID, "name": "verified-facts", "status": "AVAILABLE",
         "updatedAt": NOW}]}, {"knowledgeBaseId": KB_ID, "maxResults": 100})


def bucket_hardening(s: StubSession) -> None:
    s3 = s.stub("s3")
    s3.add_response("put_public_access_block", {}, {"Bucket": BUCKET, "PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True}})
    s3.add_response("put_bucket_encryption", {}, {"Bucket": BUCKET, "ServerSideEncryptionConfiguration": {
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}})
    s3.add_response("put_bucket_versioning", {}, {"Bucket": BUCKET,
                                                  "VersioningConfiguration": {"Status": "Enabled"}})
    s3.add_response("put_bucket_ownership_controls", {}, {"Bucket": BUCKET, "OwnershipControls": {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}})
    s3.add_response("put_bucket_policy", {}, {"Bucket": BUCKET, "Policy": json.dumps(kp.bucket_policy(names()))})
    s3.add_response("put_bucket_lifecycle_configuration", {}, {
        "Bucket": BUCKET, "LifecycleConfiguration": kp.LIFECYCLE_CONFIGURATION})
    s3.add_response("put_bucket_tagging", {}, {"Bucket": BUCKET, "Tagging": {"TagSet": [
        {"Key": "owner", "Value": "platform-team"}, {"Key": "project", "Value": "validated-kb"}]}})


def status_calls(s: StubSession, *, bucket: bool = True, vectors: bool = True, role_exists: bool = True,
                 kb: bool = True) -> None:
    """Responses for the read-only status sweep (after STS)."""
    if bucket:
        s.stub("s3").add_response("head_bucket", {}, {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT})
    else:
        s.stub("s3").add_client_error("head_bucket", "404", http_status_code=404)
    if vectors:
        vector_bucket(s)
        index(s)
    else:
        s.stub("s3vectors").add_client_error("get_vector_bucket", "NotFoundException", http_status_code=404)
    if role_exists:
        s.stub("iam").add_response("get_role", role(), {"RoleName": ROLE})
    else:
        s.stub("iam").add_client_error("get_role", "NoSuchEntity", http_status_code=404)
    if kb:
        kb_listed(s)
        s.stub("bedrock-agent").add_response("get_knowledge_base", kb_body(), {"knowledgeBaseId": KB_ID})
        ds_listed(s)
        s.stub("bedrock-agent").add_response("list_ingestion_jobs", {"ingestionJobSummaries": []}, {
            "knowledgeBaseId": KB_ID, "dataSourceId": DS_ID, "maxResults": 1,
            "sortBy": {"attribute": "STARTED_AT", "order": "DESCENDING"}})
    else:
        s.stub("bedrock-agent").add_response("list_knowledge_bases", {"knowledgeBaseSummaries": []},
                                             {"maxResults": 100})


# ------------------------------------------------------------------------------ config validation


def test_validate_config_defaults_derive_every_name() -> None:
    cfg = kp.validate_config({"region": "ap-southeast-2", "name_prefix": "team1"})
    assert cfg.kb_name == "team1-validated-kb"
    assert cfg.role_name == "team1-kb-role-ap-southeast-2"
    assert cfg.embedding_model_id == "amazon.titan-embed-text-v2:0"
    assert cfg.index_name == "verified-facts" and cfg.inclusion_prefix == "verified/"
    assert cfg.retain_data is True and cfg.tags == {} and cfg.iam_wait_seconds == 15


@pytest.mark.parametrize(("patch", "fragment"), [
    ({"region": None}, "'region' is required"),
    ({"name_prefix": None}, "'name_prefix' is required"),
    ({"region": "Europe"}, "not an AWS Region"),
    ({"name_prefix": "Bad_Prefix"}, "name_prefix"),
    ({"name_prefix": "a--b"}, "name_prefix"),
    ({"name_prefix": "x" * 21}, "name_prefix"),
    ({"retain_data": "no"}, "retain_data"),
    ({"tags": {"mcp-multirag-kb": "true"}}, "set automatically"),
    ({"tags": {"aws:owner": "x"}}, "reserved"),
    ({"tags": ["owner"]}, "'tags' must be an object"),
    ({"inclusion_prefix": "/verified"}, "inclusion_prefix"),
    ({"index_name": "Verified_Facts"}, "index_name"),
    ({"kb_name": "has space"}, "kb_name"),
    ({"expected_account_id": "12"}, "12-digit"),
    ({"iam_wait_seconds": 999}, "iam_wait_seconds"),
    ({"bucket_name": "UPPER"}, "bucket_name"),
    ({"regoin": "eu-west-1"}, "unknown key(s): regoin"),
])
def test_validate_config_rejects_bad_values(patch: dict[str, Any], fragment: str) -> None:
    raw: dict[str, Any] = {"region": "eu-west-1", "name_prefix": "team1"}
    raw.update(patch)
    raw = {k: v for k, v in raw.items() if v is not None}
    with pytest.raises(kp.KbConfigError) as err:
        kp.validate_config(raw)
    assert fragment in str(err.value)
    assert err.value.exit_code == 2


def test_load_config_tolerates_bom_and_rejects_bad_json(tmp_path: Path) -> None:
    good = tmp_path / "c.json"
    good.write_bytes(b"\xef\xbb\xbf" + json.dumps({"region": REGION, "name_prefix": PREFIX}).encode())
    assert kp.load_config(good)["name_prefix"] == PREFIX
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(kp.KbConfigError, match="not valid JSON"):
        kp.load_config(bad)
    with pytest.raises(kp.KbConfigError, match="not found"):
        kp.load_config(tmp_path / "missing.json")


def test_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "infra" / "config" / "kb-config.example.json"
    cfg = kp.validate_config(kp.load_config(example))
    assert cfg.embedding_model_id == "amazon.titan-embed-text-v2:0"
    assert example.read_bytes()[:3] != b"\xef\xbb\xbf"


def test_bucket_policy_denies_insecure_transport_only() -> None:
    policy = kp.bucket_policy(names())
    stmts = {s["Sid"]: s for s in policy["Statement"]}
    assert set(stmts) == {"DenyInsecureTransport", "DenyOutdatedTls"}
    assert all(s["Effect"] == "Deny" for s in stmts.values())  # no Allow: the bucket never becomes public
    for s in stmts.values():
        assert s["Resource"] == [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"]
    assert stmts["DenyInsecureTransport"]["Condition"] == {"Bool": {"aws:SecureTransport": False}}
    assert stmts["DenyOutdatedTls"]["Condition"] == {"NumericLessThan": {"s3:TlsVersion": "1.2"}}
    rule = kp.LIFECYCLE_CONFIGURATION["Rules"][0]
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 7}
    assert "NoncurrentVersionExpiration" not in rule and "Expiration" not in rule  # versions are the restore path


def test_policies_are_scoped() -> None:
    names_ = names()
    trust = kp.trust_policy(names_, REGION)["Statement"][0]
    assert trust["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    assert trust["Condition"]["ArnLike"]["aws:SourceArn"] == \
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/*"
    stmts = {s["Sid"]: s for s in kp.role_policy(names_, VB_ARN, IDX_ARN)["Statement"]}
    assert stmts["InvokeEmbeddingModel"]["Resource"] == [MODEL_ARN]
    assert stmts["ListFactsBucket"]["Resource"] == [f"arn:aws:s3:::{BUCKET}"]
    assert stmts["ReadFacts"]["Resource"] == [f"arn:aws:s3:::{BUCKET}/*"]
    assert stmts["VectorIndex"]["Resource"] == [VB_ARN, IDX_ARN]
    assert all("*" not in a for s in stmts.values() for a in s["Action"])


# ------------------------------------------------------------------------------ provision


def test_provision_creates_everything_from_empty(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    preflight_ok(s)
    s3 = s.stub("s3")
    s3.add_client_error("head_bucket", "404", http_status_code=404)
    s3.add_response("create_bucket", {"Location": f"/{BUCKET}"}, {
        "Bucket": BUCKET, "CreateBucketConfiguration": {"LocationConstraint": REGION}})
    bucket_hardening(s)
    v = s.stub("s3vectors")
    v.add_client_error("get_vector_bucket", "NotFoundException", http_status_code=404)
    v.add_response("create_vector_bucket", {"vectorBucketArn": VB_ARN}, {"vectorBucketName": VBUCKET, "tags": TAGS})
    vector_bucket(s)
    v.add_client_error("get_index", "NotFoundException", http_status_code=404)
    v.add_response("create_index", {"indexArn": IDX_ARN}, {
        "vectorBucketName": VBUCKET, "indexName": "verified-facts", "dataType": "float32", "dimension": 1024,
        "distanceMetric": "cosine", "tags": TAGS,
        "metadataConfiguration": {"nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]}})
    index(s)
    i = s.stub("iam")
    i.add_client_error("get_role", "NoSuchEntity", http_status_code=404)
    i.add_response("create_role", role(), {"RoleName": ROLE, "AssumeRolePolicyDocument": ANY,
                                           "Description": ANY, "Tags": ANY})
    i.add_response("put_role_policy", {}, {"RoleName": ROLE, "PolicyName": "kb-access", "PolicyDocument": ANY})
    a = s.stub("bedrock-agent")
    a.add_response("list_knowledge_bases", {"knowledgeBaseSummaries": []}, {"maxResults": 100})
    a.add_client_error("create_knowledge_base", "ValidationException",
                       service_message="The role could not be assumed by Bedrock", http_status_code=400)
    a.add_response("create_knowledge_base", kb_body("CREATING"), {
        "name": KB_NAME, "roleArn": ROLE_ARN, "description": ANY,
        "knowledgeBaseConfiguration": {"type": "VECTOR",
                                       "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": MODEL_ARN}},
        "storageConfiguration": {"type": "S3_VECTORS", "s3VectorsConfiguration": {"indexArn": IDX_ARN}},
        "tags": {**TAGS, "mcp-multirag-kb": "true"}})
    a.add_response("get_knowledge_base", kb_body("CREATING"), {"knowledgeBaseId": KB_ID})
    a.add_response("get_knowledge_base", kb_body("ACTIVE"), {"knowledgeBaseId": KB_ID})
    a.add_response("list_data_sources", {"dataSourceSummaries": []}, {"knowledgeBaseId": KB_ID, "maxResults": 100})
    a.add_response("create_data_source", {"dataSource": {
        "knowledgeBaseId": KB_ID, "dataSourceId": DS_ID, "name": "verified-facts", "status": "AVAILABLE",
        "dataSourceConfiguration": {"type": "S3", "s3Configuration": {"bucketArn": f"arn:aws:s3:::{BUCKET}"}},
        "createdAt": NOW, "updatedAt": NOW}}, {
        "knowledgeBaseId": KB_ID, "name": "verified-facts", "description": ANY,
        "dataSourceConfiguration": {"type": "S3", "s3Configuration": {
            "bucketArn": f"arn:aws:s3:::{BUCKET}", "inclusionPrefixes": ["verified/"],
            "bucketOwnerAccountId": ACCOUNT}},
        "vectorIngestionConfiguration": {"chunkingConfiguration": {"chunkingStrategy": "NONE"}},
        "dataDeletionPolicy": "DELETE"})
    sleeps: list[float] = []
    cfg = base_config(tmp_path)

    state = kp.provision(cfg, session=s, sleep=sleeps.append, log=lambda _m: None)

    s.assert_done()
    assert STATE_KEYS <= set(state)
    assert state["kb_id"] == KB_ID and state["data_source_id"] == DS_ID
    assert state["index_arn"] == IDX_ARN and state["role_arn"] == ROLE_ARN and state["bucket"] == BUCKET
    assert state["embedding_model"] == MODEL_ARN
    assert sleeps[0] == 15  # IAM propagation wait after creating the role
    assert kp.KB_CREATE_RETRY_SECONDS in sleeps  # retried once while the role propagated
    raw = Path(cfg["state_file"]).read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf" and json.loads(raw.decode("utf-8"))["kb_id"] == KB_ID
    policy = json.loads(s.params["iam.PutRolePolicy"]["PolicyDocument"])
    assert {st["Sid"]: st["Resource"] for st in policy["Statement"]}["VectorIndex"] == [VB_ARN, IDX_ARN]
    trust = json.loads(s.params["iam.CreateRole"]["AssumeRolePolicyDocument"])
    assert trust["Statement"][0]["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}


def test_provision_rerun_reuses_everything(tmp_path: Path) -> None:
    s = StubSession(region="us-east-1")
    us_bucket = f"{PREFIX}-kb-{ACCOUNT}-us-east-1"
    s.stub("sts").add_response("get_caller_identity", {"Account": ACCOUNT,
                                                       "Arn": f"arn:aws:iam::{ACCOUNT}:user/deployer"}, {})
    preflight_ok(s)
    s3 = s.stub("s3")
    s3.add_response("head_bucket", {}, {"Bucket": us_bucket, "ExpectedBucketOwner": ACCOUNT})
    for op in ("put_public_access_block", "put_bucket_encryption", "put_bucket_versioning",
               "put_bucket_ownership_controls", "put_bucket_policy", "put_bucket_lifecycle_configuration"):
        s3.add_response(op, {}, None)
    v = s.stub("s3vectors")
    us_vb = f"{PREFIX}-vectors-{ACCOUNT}-us-east-1"
    us_vb_arn = f"arn:aws:s3vectors:us-east-1:{ACCOUNT}:bucket/{us_vb}"
    us_idx = f"{us_vb_arn}/index/verified-facts"
    v.add_response("get_vector_bucket", {"vectorBucket": {"vectorBucketName": us_vb, "vectorBucketArn": us_vb_arn,
                                                          "creationTime": NOW}}, {"vectorBucketName": us_vb})
    v.add_response("get_index", {"index": {
        "vectorBucketName": us_vb, "indexName": "verified-facts", "indexArn": us_idx, "creationTime": NOW,
        "dataType": "float32", "dimension": 1024, "distanceMetric": "cosine",
        "metadataConfiguration": {"nonFilterableMetadataKeys": list(kp.NON_FILTERABLE_KEYS)}}}, None)
    i = s.stub("iam")
    i.add_response("get_role", role(), {"RoleName": f"{PREFIX}-kb-role-us-east-1"})
    i.add_response("update_assume_role_policy", {}, None)
    i.add_response("put_role_policy", {}, None)
    a = s.stub("bedrock-agent")
    kb_listed(s)
    body = kb_body()
    body["knowledgeBase"]["storageConfiguration"]["s3VectorsConfiguration"]["indexArn"] = us_idx
    a.add_response("get_knowledge_base", body, {"knowledgeBaseId": KB_ID})
    a.add_response("get_knowledge_base", body, {"knowledgeBaseId": KB_ID})
    ds_listed(s)
    sleeps: list[float] = []
    cfg = base_config(tmp_path, region="us-east-1", tags={})

    state = kp.provision(cfg, session=s, sleep=sleeps.append, log=lambda _m: None)

    s.assert_done()
    assert not [c for c in s.calls if ".Create" in c], s.calls
    assert sleeps == []
    assert state["kb_id"] == KB_ID and state["data_source_id"] == DS_ID and state["index_arn"] == us_idx


def test_provision_refuses_incompatible_existing_index(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    preflight_ok(s)
    s.stub("s3").add_response("head_bucket", {}, None)
    bucket_hardening(s)
    vector_bucket(s)
    index(s, keys=("other",))
    with pytest.raises(kp.KbProvisionError, match="cannot be reused"):
        kp.provision(base_config(tmp_path), session=s, sleep=lambda _x: None, log=lambda _m: None)


def test_provision_reports_missing_model_access(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    s.stub("s3vectors").add_response("list_vector_buckets", {"vectorBuckets": []}, None)
    s.stub("bedrock-agent").add_response("list_knowledge_bases", {"knowledgeBaseSummaries": []}, None)
    model_ok(s, entitlementAvailability="NOT_AVAILABLE")
    with pytest.raises(kp.KbModelAccessError, match="model access not enabled"):
        kp.provision(base_config(tmp_path), session=s, log=lambda _m: None)
    assert not [c for c in s.calls if ".Create" in c or c.startswith("s3.")]


def test_provision_reports_missing_permissions(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    s.stub("s3vectors").add_client_error("list_vector_buckets", "AccessDeniedException",
                                         service_message="not authorized", http_status_code=403)
    with pytest.raises(kp.KbPermissionError, match="deployer-policy.json"):
        kp.provision(base_config(tmp_path), session=s, log=lambda _m: None)


def test_provision_reports_unsupported_region(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    client = s.client("s3vectors")
    with mock.patch.object(client, "list_vector_buckets",
                           side_effect=EndpointConnectionError(endpoint_url="https://s3vectors.example")):
        with pytest.raises(kp.KbRegionUnsupportedError, match="S3 Vectors"):
            kp.provision(base_config(tmp_path), session=s, log=lambda _m: None)


def test_provision_refuses_wrong_account(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    with pytest.raises(kp.KbProvisionError, match="expects 999999999999"):
        kp.provision(base_config(tmp_path, expected_account_id="999999999999"), session=s, log=lambda _m: None)


def test_status_is_read_only(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s)
    out = kp.status(base_config(tmp_path), session=s)
    s.assert_done()
    assert out["ready"] is True
    assert out["resources"]["knowledge_base"]["id"] == KB_ID
    assert all(c.split(".")[1].startswith(("Get", "List", "Head")) for c in s.calls), s.calls


# ------------------------------------------------------------------------------ teardown


def _delete_calls(s: StubSession, *, bucket: bool) -> None:
    a = s.stub("bedrock-agent")
    a.add_response("delete_data_source", {"knowledgeBaseId": KB_ID, "dataSourceId": DS_ID, "status": "DELETING"},
                   {"knowledgeBaseId": KB_ID, "dataSourceId": DS_ID})
    a.add_client_error("get_data_source", "ResourceNotFoundException", http_status_code=404)
    a.add_response("delete_knowledge_base", {"knowledgeBaseId": KB_ID, "status": "DELETING"},
                   {"knowledgeBaseId": KB_ID})
    a.add_response("get_knowledge_base", kb_body("DELETING"), {"knowledgeBaseId": KB_ID})
    a.add_client_error("get_knowledge_base", "ResourceNotFoundException", http_status_code=404)
    v = s.stub("s3vectors")
    v.add_response("delete_index", {}, {"vectorBucketName": VBUCKET, "indexName": "verified-facts"})
    v.add_response("delete_vector_bucket", {}, {"vectorBucketName": VBUCKET})
    i = s.stub("iam")
    i.add_response("delete_role_policy", {}, {"RoleName": ROLE, "PolicyName": "kb-access"})
    i.add_response("delete_role", {}, {"RoleName": ROLE})
    if bucket:
        s.stub("s3").add_response("delete_bucket", {}, {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT})


DELETE_ORDER = ["bedrock-agent.DeleteDataSource", "bedrock-agent.DeleteKnowledgeBase", "s3vectors.DeleteIndex",
                "s3vectors.DeleteVectorBucket", "iam.DeleteRolePolicy", "iam.DeleteRole"]


def _deletes(s: StubSession) -> list[str]:
    return [c for c in s.calls if ".Delete" in c]


def test_teardown_without_confirm_is_a_dry_run(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s)
    out = kp.teardown(base_config(tmp_path, retain_data=False), session=s, log=lambda _m: None)
    s.assert_done()
    assert out["dry_run"] is True and out["deleted"] == []
    assert out["plan"][0].startswith("data source") and out["plan"][-1] == f"bucket {BUCKET}"
    assert _deletes(s) == []
    assert not Path(base_config(tmp_path)["state_file"]).exists()


def test_teardown_deletes_in_dependency_order_and_bucket_last(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s)
    s.stub("s3").add_response("list_object_versions", {"Versions": [], "DeleteMarkers": []},
                              {"Bucket": BUCKET, "MaxKeys": 1})
    _delete_calls(s, bucket=True)
    cfg = base_config(tmp_path, retain_data=False)
    out = kp.teardown(cfg, confirm=True, session=s, sleep=lambda _x: None, log=lambda _m: None)
    s.assert_done()
    assert _deletes(s) == DELETE_ORDER + ["s3.DeleteBucket"]
    assert out["deleted"][-1] == f"bucket {BUCKET}" and out["retained"] == []
    state = json.loads(Path(cfg["state_file"]).read_text(encoding="utf-8"))
    assert "kb_id" not in state and "bucket" not in state


def test_teardown_keeps_bucket_when_retain_data(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s)
    _delete_calls(s, bucket=False)
    cfg = base_config(tmp_path, retain_data=True)
    out = kp.teardown(cfg, confirm=True, session=s, sleep=lambda _x: None, log=lambda _m: None)
    s.assert_done()
    assert _deletes(s) == DELETE_ORDER
    assert out["retained"] == [f"bucket {BUCKET} (retain_data is true)"]
    assert json.loads(Path(cfg["state_file"]).read_text(encoding="utf-8"))["bucket"] == BUCKET


def test_teardown_refuses_non_empty_bucket_without_force(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s)
    s.stub("s3").add_response("list_object_versions", {"Versions": [
        {"Key": "verified/a.md", "VersionId": "v1"}]}, {"Bucket": BUCKET, "MaxKeys": 1})
    with pytest.raises(kp.KbTeardownRefused, match="nothing was deleted"):
        kp.teardown(base_config(tmp_path, retain_data=False), confirm=True, session=s,
                    sleep=lambda _x: None, log=lambda _m: None)
    assert _deletes(s) == []


def test_teardown_force_empties_versioned_bucket_then_deletes_it(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s)
    _delete_calls(s, bucket=False)
    s3 = s.stub("s3")
    s3.add_response("list_object_versions", {
        "IsTruncated": True, "NextKeyMarker": "verified/a.md", "NextVersionIdMarker": "v1",
        "Versions": [{"Key": "verified/a.md", "VersionId": "v1"}]}, {"Bucket": BUCKET})
    s3.add_response("delete_objects", {}, {"Bucket": BUCKET, "Delete": {
        "Objects": [{"Key": "verified/a.md", "VersionId": "v1"}], "Quiet": True}})
    s3.add_response("list_object_versions", {"IsTruncated": False, "DeleteMarkers": [
        {"Key": "verified/a.md", "VersionId": "d1"}]},
        {"Bucket": BUCKET, "KeyMarker": "verified/a.md", "VersionIdMarker": "v1"})
    s3.add_response("delete_objects", {}, {"Bucket": BUCKET, "Delete": {
        "Objects": [{"Key": "verified/a.md", "VersionId": "d1"}], "Quiet": True}})
    s3.add_response("delete_bucket", {}, {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT})
    kp.teardown(base_config(tmp_path, retain_data=False), confirm=True, force=True, session=s,
                sleep=lambda _x: None, log=lambda _m: None)
    s.assert_done()
    assert _deletes(s) == DELETE_ORDER + ["s3.DeleteObjects", "s3.DeleteObjects", "s3.DeleteBucket"]


def test_teardown_is_idempotent_when_everything_is_gone(tmp_path: Path) -> None:
    s = StubSession()
    sts(s)
    status_calls(s, bucket=False, vectors=False, role_exists=False, kb=False)
    out = kp.teardown(base_config(tmp_path, retain_data=False), confirm=True, session=s,
                      sleep=lambda _x: None, log=lambda _m: None)
    s.assert_done()
    assert out["plan"] == [] and out["deleted"] == [] and _deletes(s) == []


# ------------------------------------------------------------------------------ CLI wiring (bcp kb ...)


def _write_config(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "kb-config.json"
    path.write_text(json.dumps(base_config(tmp_path, **extra)), encoding="utf-8")
    return path


def test_cli_kb_help_lists_provision_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    from bcp import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["kb", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("export", "sync", "audit", "check-retrieval", "stale", "hook", "provision", "status", "teardown"):
        assert name in out, name


def test_cli_kb_teardown_without_confirm_prints_dry_run(tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    from bcp import cli

    s = StubSession()
    sts(s)
    status_calls(s)
    path = _write_config(tmp_path, retain_data=False)
    with mock.patch.object(kp, "_session", lambda _cfg, _session: s):
        assert cli.main(["kb", "teardown", "--config", str(path)]) == 0
    s.assert_done()
    out = capsys.readouterr().out
    assert "kb teardown (dry run, pass --confirm to delete)" in out
    assert f"plan: bucket {BUCKET}" in out and "deleted:" not in out
    assert _deletes(s) == []


def test_cli_kb_provision_bad_config_exits_with_input_error(tmp_path: Path) -> None:
    from bcp import cli

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"region": "Europe", "name_prefix": PREFIX}), encoding="utf-8")
    with mock.patch.object(kp, "_session", side_effect=AssertionError("no AWS call expected")):
        assert cli.main(["kb", "provision", "--config", str(bad)]) == 2
        assert cli.main(["kb", "provision", "--config", str(tmp_path / "missing.json")]) == 2


def test_cli_kb_status_json_prints_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from bcp import cli

    s = StubSession()
    sts(s)
    status_calls(s)
    path = _write_config(tmp_path)
    with mock.patch.object(kp, "_session", lambda _cfg, _session: s):
        assert cli.main(["kb", "status", "--config", str(path), "--json"]) == 0
    s.assert_done()
    data = json.loads(capsys.readouterr().out)
    assert data["ready"] is True
    assert data["resources"]["knowledge_base"]["id"] == KB_ID