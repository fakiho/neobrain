"""Fixture test for the OpenCode adapter's USER.md compaction.

`compactUserDoc` lives in the TypeScript plugin
(``adapters/opencode/neoBrain-memory/index.ts``), so this test bridges to Node
(type-stripping, no build step) and runs the real function over a grown USER.md
fixture. It pins the contract: keep the active directive bullets, drop the
header/format explainer (including its fenced sample), dated metadata comments,
superseded entries, inline ``[pin]`` markers and the ``Related`` footer.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "adapters" / "opencode" / "neoBrain-memory" / "index.ts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "user_md_grown.md"

# The node one-liner: import the plugin module, compact stdin, print the result.
_NODE = (
    'const fs = require("node:fs");'
    "import({url}).then((m) => {{"
    'const text = fs.readFileSync(0, "utf8");'
    "process.stdout.write(m.compactUserDoc(text));"
    "}}).catch((e) => {{ console.error((e && e.stack) || e); process.exit(1); }});"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compact_user_md_keeps_only_active_directives():
    script = _NODE.format(url=json.dumps(PLUGIN.as_uri()))
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "-e", script],
        input=FIXTURE.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout

    # Exact: the active bullets, in file order, with the pin markers stripped.
    assert out == "\n".join(
        [
            "- Always spawn subagents with `visible=true`.",
            "- Prefer an elegant, dark, low-saturation interface.",
            "- Always communicate straight to the point.",
            "- Always write completed work down.",
        ]
    )

    # Boilerplate and dead entries are gone.
    assert "concise progress updates" not in out  # fenced sample, not a real entry
    assert "Begin each directive" not in out  # format explainer
    assert "TypeScript over Python" not in out  # superseded
    assert "Agent workspace" not in out  # Related footer
    assert "[pin]" not in out and "<!--" not in out
    assert len(out) < 4000
