from __future__ import annotations
import argparse, json, os
from pathlib import Path
from ..providers.alpaca_sip_non_active_cutover import CONFIRMATION_TOKEN, build_cutover_plan, execute_cutover
def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description="Plan or execute the non-active Alpaca SIP cutover"); parser.add_argument("--execute",action="store_true"); parser.add_argument("--approved-plan-id"); args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[3]; plan=build_cutover_plan(repo_root=root)
    if not args.execute: print(json.dumps({"mode":"PLAN_ONLY_NO_WRITES","cutover_plan":plan},indent=2,sort_keys=True)); return 0
    if not args.approved_plan_id: parser.error("--execute requires --approved-plan-id")
    print(json.dumps(execute_cutover(approved_plan_id=args.approved_plan_id,owner_confirmation=os.environ.get(CONFIRMATION_TOKEN,""),repo_root=root),indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
