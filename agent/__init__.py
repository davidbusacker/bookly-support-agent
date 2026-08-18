"""
Interview-friendly agent core — read these files top-to-bottom:

  pipeline.py   One customer message: guardrails → trace → agent → closeout
  loop.py       Claude tool-use loop (stream → tools → repeat)
  session.py    In-memory state, identity, caller history, system prompt
  traces.py     Bookly admin trace create/append (background)
  config.py     Models, clients, thresholds (plumbing)
"""
