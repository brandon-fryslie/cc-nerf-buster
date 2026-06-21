# probe.py / report.py / crossings.py are script-mode modules: probe.sh runs
# them with this directory as sys.path[0], so they import each other by bare
# name (`from crossings import ...`). pytest collects this package from the
# repo-root rootdir, which puts `tools/` — not this directory — on sys.path,
# so those bare imports would not resolve. Restore the script-mode entry so
# the tests load the modules exactly as probe.sh does. [LAW:single-enforcer]
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
