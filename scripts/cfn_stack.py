"""Deploy, inspect and delete the validated-kb CloudFormation stack with boto3 (route B).

Why a script: the template uses the ``AWS::LanguageExtensions`` transform, so it must go through a
change set, and Windows quoting of ``--parameter-overrides`` is error-prone. Any AWS CLI v2 also
works (see README.md); this script needs only boto3.

Usage::

    python scripts/cfn_stack.py deploy --stack-name <prefix>-validated-kb --region <region> \
        --param NamePrefix=<prefix> --param Owner=<team> [--param RetainData=true] [--tag key=value]
    python scripts/cfn_stack.py state  --stack-name <prefix>-validated-kb --region <region> --out kb_state.json
    python scripts/cfn_stack.py delete --stack-name <prefix>-validated-kb --region <region> --confirm

``deploy`` creates the stack or updates it (no-op when nothing changed) and waits. ``state`` writes the
state file that ``bcp kb sync|stale|audit|check-retrieval`` and ``scripts/smoke_test.py`` read (UTF-8,
no BOM). ``delete`` without ``--confirm`` only prints what it would delete.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "validated-kb.yaml"
CAPABILITIES = ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
TERMINAL_OK = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}


def _pairs(items: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"{what} must be KEY=VALUE: {item!r}")
        out[key] = value
    return out


def _client(service: str, region: str) -> Any:
    import boto3  # imported here so --help works without boto3

    return boto3.client(service, region_name=region)


def _stack(cfn: Any, name: str) -> dict[str, Any] | None:
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except Exception as exc:  # botocore ClientError "does not exist"
        if "does not exist" in str(exc):
            return None
        raise


def _events(cfn: Any, name: str, limit: int = 15) -> list[str]:
    rows = cfn.describe_stack_events(StackName=name)["StackEvents"][:limit]
    return [f"{e['Timestamp']:%H:%M:%S} {e['LogicalResourceId']} {e['ResourceStatus']} "
            f"{e.get('ResourceStatusReason', '')}".strip() for e in rows]


def deploy(name: str, region: str, params: dict[str, str], tags: dict[str, str]) -> int:
    cfn = _client("cloudformation", region)
    existing = _stack(cfn, name)
    if existing and existing["StackStatus"] == "ROLLBACK_COMPLETE":
        print(f"stack {name} is ROLLBACK_COMPLETE; delete it first (scripts/cfn_stack.py delete ... --confirm)",
              file=sys.stderr)
        return 1
    kind = "UPDATE" if existing else "CREATE"
    change_set = f"deploy-{int(time.time())}"
    cfn.create_change_set(
        StackName=name, ChangeSetName=change_set, ChangeSetType=kind,
        TemplateBody=TEMPLATE.read_text(encoding="utf-8"), Capabilities=CAPABILITIES,
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in sorted(params.items())],
        Tags=[{"Key": k, "Value": v} for k, v in sorted(tags.items())])
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=name, ChangeSetName=change_set, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except Exception:
        desc = cfn.describe_change_set(StackName=name, ChangeSetName=change_set)
        reason = desc.get("StatusReason", "")
        if "didn't contain changes" in reason or "No updates" in reason:
            cfn.delete_change_set(StackName=name, ChangeSetName=change_set)
            print(f"stack {name}: no changes")
            return 0
        print(f"change set failed: {desc.get('Status')} {reason}", file=sys.stderr)
        return 1
    print(f"{kind.lower()} change set ready; executing", flush=True)
    cfn.execute_change_set(StackName=name, ChangeSetName=change_set)
    waiter = "stack_create_complete" if kind == "CREATE" else "stack_update_complete"
    try:
        cfn.get_waiter(waiter).wait(StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 180})
    except Exception as exc:
        print(f"stack {name} did not complete: {exc}", file=sys.stderr)
        for line in _events(cfn, name):
            print("  " + line, file=sys.stderr)
        return 1
    stack = _stack(cfn, name) or {}
    print(json.dumps({"StackId": stack.get("StackId"), "StackStatus": stack.get("StackStatus"),
                      "Outputs": {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}}, indent=2))
    return 0 if stack.get("StackStatus") in TERMINAL_OK else 1


def write_state(name: str, region: str, out: Path) -> int:
    cfn = _client("cloudformation", region)
    stack = _stack(cfn, name)
    if not stack:
        print(f"stack {name} not found in {region}", file=sys.stderr)
        return 1
    o = {x["OutputKey"]: x["OutputValue"] for x in stack.get("Outputs", [])}
    account = _client("sts", region).get_caller_identity()["Account"]
    state = {
        "schema": "bcp.kb-state/1", "source": f"cloudformation:{name}", "region": region, "account": account,
        "bucket": o["BucketName"], "bucket_arn": f"arn:aws:s3:::{o['BucketName']}",
        "vector_bucket": o["VectorBucketArn"].rsplit("/", 1)[-1], "vector_bucket_arn": o["VectorBucketArn"],
        "index": o["IndexArn"].rsplit("/", 1)[-1], "index_arn": o["IndexArn"], "role_arn": o["RoleArn"],
        "kb_id": o["KnowledgeBaseId"], "data_source_id": o["DataSourceId"],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")  # UTF-8 without BOM
    print(f"wrote {out}")
    return 0


def delete(name: str, region: str, confirm: bool) -> int:
    cfn = _client("cloudformation", region)
    stack = _stack(cfn, name)
    if not stack:
        print(f"stack {name} not found in {region}; nothing to delete")
        return 0
    resources = cfn.describe_stack_resources(StackName=name)["StackResources"]
    for r in resources:
        print(f"  {r['ResourceType']:<32} {r.get('PhysicalResourceId', '')}")
    if not confirm:
        print(f"dry run: would delete stack {name} and the {len(resources)} resources above "
              "(a bucket with DeletionPolicy Retain is kept). Add --confirm.")
        return 0
    stack_id = stack["StackId"]
    cfn.delete_stack(StackName=name)
    try:
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack_id, WaiterConfig={"Delay": 10,
                                                                                      "MaxAttempts": 180})
    except Exception as exc:
        print(f"delete did not complete: {exc}", file=sys.stderr)
        for line in _events(cfn, stack_id):
            print("  " + line, file=sys.stderr)
        return 1
    final = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
    print(f"stack {name}: {final}")
    return 0 if final == "DELETE_COMPLETE" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("deploy", "state", "delete"):
        p = sub.add_parser(cmd)
        p.add_argument("--stack-name", required=True)
        p.add_argument("--region", required=True)
        if cmd == "deploy":
            p.add_argument("--param", action="append", help="ParameterKey=Value (repeat)")
            p.add_argument("--tag", action="append", help="stack tag key=value (repeat)")
        if cmd == "state":
            p.add_argument("--out", type=Path, required=True)
        if cmd == "delete":
            p.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "deploy":
        return deploy(args.stack_name, args.region, _pairs(args.param, "--param"), _pairs(args.tag, "--tag"))
    if args.cmd == "state":
        return write_state(args.stack_name, args.region, args.out)
    return delete(args.stack_name, args.region, args.confirm)


if __name__ == "__main__":
    sys.exit(main())
