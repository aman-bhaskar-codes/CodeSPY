"""Owner-run live check of the report narrative (makes a few small paid Bedrock calls).

    CODESPY_LIVE_BEDROCK=1 AWS_PROFILE=<profile> uv run --project backend \
        python scripts/narrative_smoke.py --converse-model <model-or-inference-profile-id> [--demo]

This does not need the platform. It builds the narrative pages from a stored report (``--report-file``,
the JSON of GET .../architecture-reports/{id}) or from a small built-in demo repository (``--demo``),
sends each page to Bedrock Converse in ap-south-1 with the versioned prompt assets, parses the reply with
the production parser and verifies it with the production verifier. It prints per page: statements,
sentences, confirmed sentences, tokens and latency; and in total: the verified ratio and the prompt
revision. It never prints the prompt, the reply text or credentials. At most ``--max-pages`` calls are
made (default 3, each at most 1500 output tokens). Refuses to run without the opt-in.

Pass criteria for the owner: parse failures near zero, verified ratio at or above 0.8, and no confirmed
sentence that states something the cited statements do not.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

MAX_OUTPUT_TOKENS = 1500
DEMO_FILES = {
    "app/__init__.py": "",
    "app/web.py": (
        "import os\nfrom flask import Flask\nfrom app import models\nfrom app.util import helper\n"
        'app = Flask(__name__)\n@app.route("/users")\ndef users(): return helper()\n'
    ),
    "app/models.py": "from sqlalchemy import Column\nclass User(Base):\n    __tablename__ = 'users'\n",
    "app/util.py": "import ghost_internal\ndef helper(): return 1\n",
    "main.py": "from app import web\nimport requests\n",
}


def demo_report():
    from app.analyzers.facts import extract_facts
    from app.analyzers.graph import FileEntry
    from app.analyzers.graph.report import assemble_report
    from app.analyzers.registry import FACT_DIALECTS
    from app.contracts.structure import AnalysisBudget, SourceUnitV1

    entries = []
    for path, text in DEMO_FILES.items():
        data = text.encode()
        language = FACT_DIALECTS[Path(path).suffix]
        unit = SourceUnitV1(
            file_id=uuid4(),
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
            language=language,
        )
        entries.append(FileEntry(unit.file_id, path, language, extract_facts(unit, AnalysisBudget())))
    return assemble_report(
        org_id=uuid4(), repository_id=uuid4(), snapshot_id=uuid4(), files=entries,
        inventory=list(DEMO_FILES), eligible_files=len(entries), omitted_files=0, fact_runs=(uuid4(),),
    )


async def run(profile: str, model: str, report, max_pages: int) -> int:
    import aioboto3
    from app.adapters.model.bedrock import ConfiguredAsyncConverseClient
    from app.adapters.model.bedrock_client import open_bedrock_runtime
    from app.agents.prompts import boundary_marker, compact_schema, load_prompt, render
    from app.analyzers.graph.report_pages import build_page
    from app.contracts.report_narrative import NarrativeConclusionV1
    from app.contracts.source_view import PrivacyPolicyV1
    from app.services.investigation_tools import REPORT_SECTIONS
    from app.services.narrative_parsing import NarrativeParseError, parse_conclusion
    from app.services.narrative_verification import verify_narrative

    asset = load_prompt("report_narrative", "v1")
    digest = report.digest()
    policy = PrivacyPolicyV1()
    pages = []
    for section in REPORT_SECTIONS:
        offset = 0
        while len(pages) < max_pages:
            page = build_page(report, section, offset, policy)
            if page is None or not page.entries:
                break
            pages.append(page)
            if page.next_offset is None:
                break
            offset = page.next_offset
    shown: dict[str, dict] = {}
    sentences = []
    unparsed = 0
    session = aioboto3.Session(profile_name=profile)
    async with open_bedrock_runtime(session) as client:
        for unit, page in enumerate(pages):
            marker = boundary_marker(f"smoke-{unit}", digest)
            user = render(asset, "section.md.tmpl", marker=marker, data=page.payload,
                          section=page.section, schema=compact_schema(asset), page_number=str(unit))
            for entry in page.entries:
                shown[entry["sid"]] = entry
            started = time.perf_counter()
            reply = await ConfiguredAsyncConverseClient(client).converse(
                modelId=model,
                system=[{"text": asset.text("system.md")}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
            )
            text = "".join(b.get("text", "") for b in reply["output"]["message"]["content"])
            usage = reply.get("usage", {})
            try:
                found = parse_conclusion(text).sentences
            except NarrativeParseError as error:
                found, unparsed = (), unparsed + 1
                print(f"page {unit} {page.section}: parse failure {error.code}")
            else:
                sentences.extend(s.model_copy(update={"claim_id": f"u{unit}-{s.claim_id}"}) for s in found)
            partial = verify_narrative(NarrativeConclusionV1(sentences=tuple(found)), shown)
            print(
                f"page {unit} {page.section}: statements={len(page.entries)} sentences={len(found)} "
                f"confirmed={partial.confirmed} in={usage.get('inputTokens')} out={usage.get('outputTokens')} "
                f"stop={reply.get('stopReason')} ms={(time.perf_counter() - started) * 1000:.0f}"
            )
    outcome = verify_narrative(NarrativeConclusionV1(sentences=tuple(sentences[:50])), shown)
    print(
        f"total pages={len(pages)} sentences={outcome.total} confirmed={outcome.confirmed} "
        f"ratio={outcome.ratio:.2f} grounded={outcome.grounded} unparsed_pages={unparsed} "
        f"prompt={asset.revision}"
    )
    return 0 if outcome.grounded and not unparsed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--converse-model", required=True, help="model or inference-profile id")
    parser.add_argument("--report-file", help="JSON of a stored architecture report")
    parser.add_argument("--demo", action="store_true", help="use the built-in demo repository")
    parser.add_argument("--max-pages", type=int, default=3, help="most model calls to make (default 3)")
    args = parser.parse_args()
    if args.demo == bool(args.report_file):
        print("give exactly one of --demo or --report-file")
        return 2
    if os.environ.get("CODESPY_LIVE_BEDROCK") != "1":
        print("refusing to run: set CODESPY_LIVE_BEDROCK=1 to make small paid Bedrock calls")
        return 2
    profile = os.environ.get("AWS_PROFILE")
    if not profile:
        print("refusing to run: set AWS_PROFILE to the profile to use")
        return 2
    if not 1 <= args.max_pages <= 12:
        print("--max-pages must be between 1 and 12")
        return 2
    if args.demo:
        report = demo_report()
    else:
        from app.contracts.architecture_report import RepositoryReportV1

        report = RepositoryReportV1.model_validate_json(Path(args.report_file).read_text())
    return asyncio.run(run(profile, args.converse_model, report, args.max_pages))


if __name__ == "__main__":
    raise SystemExit(main())
