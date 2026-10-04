"""behave hooks: every scenario gets a fresh world and leaves nothing behind."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def before_all(context):
    logging.disable(logging.CRITICAL)      # keep the report readable


def before_scenario(context, scenario):
    context.world = None
    context.client = None
    context.headers = {}
    context.memo = {}


def after_scenario(context, scenario):
    if context.client is not None:
        context.client.__exit__(None, None, None)
    if context.world is not None:
        context.world.close()
