"""Offline contract checks for the paste-ready OSS persistence RAM policy."""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
import posixpath
from pathlib import Path
import unittest

from dotenv import dotenv_values

from app.sandbox_config import SandboxSettings


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_POLICY_PATH = (
    _PROJECT_ROOT / "config" / "aliyun-oss-persistence-minimal-policy.json"
)
_LIST_ACTIONS = {"oss:ListObjects"}
_OBJECT_ACTIONS = {
    "oss:GetObject",
    "oss:PutObject",
    "oss:DeleteObject",
    "oss:ListParts",
    "oss:AbortMultipartUpload",
}


def _configured_bucket_and_prefix() -> tuple[str, str]:
    values = dotenv_values(
        _PROJECT_ROOT / ".env",
        encoding="utf-8-sig",
        interpolate=False,
    )
    bucket = values.get("ALIYUN_OSS_BUCKET")
    bucket_path = values.get("ALIYUN_OSS_BUCKET_PATH")
    settings = SandboxSettings(
        oss_bucket=bucket,
        oss_bucket_path=bucket_path,
    )
    if settings.oss_bucket is None or settings.oss_bucket_path is None:
        raise AssertionError("OSS Bucket 与 bucketPath 必须在项目 .env 中配置。")
    return settings.oss_bucket, settings.oss_bucket_path.removeprefix("/")


class AliyunOssRamPolicyDocumentTests(unittest.TestCase):
    def test_policy_is_exactly_scoped_to_configured_bucket_and_prefix(self) -> None:
        bucket, prefix = _configured_bucket_and_prefix()
        policy = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))

        self.assertEqual(set(policy), {"Version", "Statement"})
        self.assertEqual(policy["Version"], "1")
        self.assertIsInstance(policy["Statement"], list)
        self.assertEqual(len(policy["Statement"]), 2)

        statements_by_actions = {
            frozenset(statement["Action"]): statement
            for statement in policy["Statement"]
        }
        self.assertEqual(
            set(statements_by_actions),
            {frozenset(_LIST_ACTIONS), frozenset(_OBJECT_ACTIONS)},
        )
        list_statement = statements_by_actions[frozenset(_LIST_ACTIONS)]
        object_statement = statements_by_actions[frozenset(_OBJECT_ACTIONS)]

        self.assertEqual(
            set(list_statement),
            {"Effect", "Action", "Resource", "Condition"},
        )
        self.assertEqual(
            set(object_statement),
            {"Effect", "Action", "Resource"},
        )
        self.assertEqual(list_statement["Effect"], "Allow")
        self.assertEqual(object_statement["Effect"], "Allow")

        bucket_resource = f"acs:oss:*:*:{bucket}"
        object_resource = f"{bucket_resource}/{prefix}/*"
        self.assertEqual(list_statement["Resource"], [bucket_resource])
        self.assertEqual(object_statement["Resource"], [object_resource])
        self.assertFalse(prefix.startswith("/"))

        expected_prefix_patterns = [prefix, f"{prefix}/*"]
        self.assertEqual(
            list_statement["Condition"],
            {"StringLike": {"oss:Prefix": expected_prefix_patterns}},
        )

        all_actions = {
            action
            for statement in policy["Statement"]
            for action in statement["Action"]
        }
        all_resources = {
            resource
            for statement in policy["Statement"]
            for resource in statement["Resource"]
        }
        self.assertNotIn("oss:*", all_actions)
        self.assertNotIn("*", all_resources)
        self.assertEqual(all_actions, _LIST_ACTIONS | _OBJECT_ACTIONS)

        parent, leaf = posixpath.split(prefix)
        sibling = posixpath.join(parent, f"{leaf}-sibling")
        self.assertNotEqual(sibling, prefix)
        self.assertFalse(sibling.startswith(f"{prefix}/"))
        for pattern in expected_prefix_patterns:
            self.assertFalse(fnmatchcase(sibling, pattern))
            self.assertFalse(fnmatchcase(f"{sibling}/probe", pattern))
        self.assertFalse(
            f"acs:oss:*:*:{bucket}/{sibling}/probe".startswith(
                object_resource.removesuffix("*")
            )
        )


if __name__ == "__main__":
    unittest.main()
