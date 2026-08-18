"""
Shared clients and knobs: models, Bookly trace client, and the sole intent/resolution cutoffs.
Pipeline.py compares to INTENT_CONFIDENCE_THRESHOLD and RESOLUTION_THRESHOLD; classifiers do not.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

import anthropic
from dotenv import load_dotenv

from aop_loader import build_system_prompt
from tools import BOOKLY_AGENT_INSTRUCTIONS, get_bookly_client, reload_bookly_client
from trace_client import TraceClient

load_dotenv()
logging.basicConfig(level=logging.INFO)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5")
INTENT_CONFIDENCE_THRESHOLD = float(os.environ.get("INTENT_CONFIDENCE_THRESHOLD", "0.50"))
RESOLUTION_THRESHOLD = float(os.environ.get("RESOLUTION_THRESHOLD", "0.90"))
TOOL_RESULT_KEEP = 2

anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
_TRACE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bookly-trace")

reload_bookly_client()
trace_client = TraceClient(get_bookly_client(), model=ANTHROPIC_MODEL)
BASE_SYSTEM_PROMPT = build_system_prompt(BOOKLY_AGENT_INSTRUCTIONS)
