import argparse
import logging
import sys
from orchestrator.utils.settings import set_setting, get_setting

def parse_args():
    parser = argparse.ArgumentParser(description="Lobs Orchestrator")
    parser.add_argument("--provider", help="Set the task provider (e.g., local)")
    parser.add_argument("--base-dir", help="Set the base directory for repositories")
    parser.add_argument("--control-repo", help="Set the path to the control repository")
    parser.add_argument("--orchestrator-repo", help="Set the path to the orchestrator repository")
    parser.add_argument("--poll-interval", type=int, help="Polling interval in seconds")
    parser.add_argument("--openclaw-executable", help="Path to openclaw executable")
    
    return parser.parse_args()

def apply_args(args):
    # Update settings if provided via CLI
    if args.provider:
        set_setting("provider", args.provider)
        logging.info(f"Provider set to: {args.provider}")
    
    if args.base_dir:
        set_setting("base_dir", args.base_dir)
        logging.info(f"Base directory set to: {args.base_dir}")

    if args.control_repo:
        set_setting("control_repo_path", args.control_repo)
        logging.info(f"Control repo path set to: {args.control_repo}")

    if args.orchestrator_repo:
        set_setting("orchestrator_repo_path", args.orchestrator_repo)
        logging.info(f"Orchestrator repo path set to: {args.orchestrator_repo}")

    if args.poll_interval:
        set_setting("poll_interval", args.poll_interval)
        logging.info(f"Poll interval set to: {args.poll_interval}")

    if args.openclaw_executable:
        set_setting("openclaw_executable", args.openclaw_executable)
        logging.info(f"OpenClaw executable set to: {args.openclaw_executable}")

def setup_orchestrator():
    args = parse_args()
    apply_args(args)
    return args
