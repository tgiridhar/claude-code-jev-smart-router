"""Benchmark task set.

Six tasks. Each one is a single prompt, a turn cap, a budget cap, a wall clock
timeout, an optional setup that seeds the working directory, a cheap
deterministic check, and a rubric for the judge.

A task that fails its objective check scores zero on functionality no matter
what the judge says. A cheap model must not be able to win on price by
producing something broken.
"""

import ast
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# check helpers
# ---------------------------------------------------------------------------

def _read(workdir, name):
    p = os.path.join(workdir, name)
    if not os.path.exists(p):
        return None
    with open(p, "r", errors="replace") as f:
        return f.read()


def _scripts(html):
    """Pull the bodies of <script> blocks that have no src attribute."""
    import re
    out = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
        if "src=" in m.group(1).lower():
            continue
        body = m.group(2).strip()
        if body:
            out.append(body)
    return out


def _node_check(js):
    """node --check on a snippet. Returns (ok, message)."""
    if not shutil.which("node"):
        return True, "skipped: node not installed"
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    try:
        r = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, "ok"
        return False, (r.stderr or r.stdout).strip().splitlines()[-1][:200]
    finally:
        os.unlink(path)


def check_html_app(artifact, min_bytes, must_contain):
    def _check(workdir):
        html = _read(workdir, artifact)
        if html is None:
            return {"ok": False, "why": f"{artifact} not created"}
        if len(html) < min_bytes:
            return {"ok": False, "why": f"{artifact} is only {len(html)} bytes"}
        if "cdn" in html.lower() or "https://unpkg" in html.lower():
            return {"ok": False, "why": "external CDN reference, brief said self-contained"}
        blocks = _scripts(html)
        if not blocks:
            return {"ok": False, "why": "no inline <script> block"}
        for i, js in enumerate(blocks):
            ok, msg = _node_check(js)
            if not ok:
                return {"ok": False, "why": f"script block {i} fails node --check: {msg}"}
        missing = [t for t in must_contain if t.lower() not in html.lower()]
        return {
            "ok": not missing,
            "why": f"missing expected feature markers: {missing}" if missing else "ok",
            "bytes": len(html),
            "script_blocks": len(blocks),
        }
    return _check


def check_svg(workdir):
    svg = _read(workdir, "pelican.svg")
    if svg is None:
        return {"ok": False, "why": "pelican.svg not created"}
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        return {"ok": False, "why": f"not well-formed XML: {e}"}
    if not root.tag.endswith("svg"):
        return {"ok": False, "why": f"root element is {root.tag}, not svg"}
    n = sum(1 for _ in root.iter())
    return {"ok": n >= 10, "why": "ok" if n >= 10 else f"only {n} elements, too sparse",
            "elements": n, "bytes": len(svg)}


def check_datasci(workdir):
    findings = _read(workdir, "FINDINGS.md")
    if findings is None:
        return {"ok": False, "why": "FINDINGS.md not created"}
    if len(findings) < 400:
        return {"ok": False, "why": f"FINDINGS.md is only {len(findings)} bytes"}
    csv = os.path.join(workdir, "sales.csv")
    if not os.path.exists(csv):
        return {"ok": False, "why": "sales.csv not created"}
    with open(csv, errors="replace") as f:
        rows = sum(1 for _ in f)
    if rows < 4000:
        return {"ok": False, "why": f"sales.csv has {rows} rows, brief said 5000"}
    for script in ("generate.py", "analyze.py"):
        src = _read(workdir, script)
        if src is None:
            return {"ok": False, "why": f"{script} not created"}
        try:
            ast.parse(src)
        except SyntaxError as e:
            return {"ok": False, "why": f"{script} does not parse: {e}"}
    return {"ok": True, "why": "ok", "csv_rows": rows, "findings_bytes": len(findings)}


def check_prose(artifact, min_bytes):
    def _check(workdir):
        txt = _read(workdir, artifact)
        if txt is None:
            return {"ok": False, "why": f"{artifact} not created"}
        if len(txt) < min_bytes:
            return {"ok": False, "why": f"{artifact} is only {len(txt)} bytes"}
        return {"ok": True, "why": "ok", "bytes": len(txt)}
    return _check


# ---------------------------------------------------------------------------
# setup helpers
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def seed_fixture(subdir, *names):
    """Copy fixture files into the run's working directory.

    Only the named files. The grading material (planted.json, test_hidden.py)
    stays out of the working directory so the agent cannot read the answers.
    """
    def _setup(workdir):
        for n in names:
            shutil.copy2(os.path.join(FIXTURES, subdir, n), os.path.join(workdir, n))
        return list(names)
    return _setup


def seed(*names):
    """Copy files from the repo into the run's working directory.

    Always a copy. The agent runs with bypassPermissions and must never be
    able to reach the real repo.
    """
    def _setup(workdir):
        for n in names:
            shutil.copy2(os.path.join(REPO, n), os.path.join(workdir, n))
        return list(names)
    return _setup


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

class Task:
    def __init__(self, name, prompt, artifact, check, rubric, requirements=(),
                 judge_files=(), max_turns=40, budget_usd=2.50, timeout_s=480,
                 setup=None, kind="code"):
        self.name = name
        self.prompt = prompt
        self.artifact = artifact
        self.check = check
        self.rubric = rubric
        self.requirements = list(requirements)
        self.judge_files = list(judge_files) or [artifact]
        self.max_turns = max_turns
        self.budget_usd = budget_usd
        self.timeout_s = timeout_s
        self.setup = setup
        self.kind = kind

    def __repr__(self):
        return f"<Task {self.name}>"


TASKS = [
    Task(
        "todo",
        prompt=(
            "Create todo.html in the current directory: a single self-contained HTML file "
            "implementing a todo list app.\n\n"
            "Requirements:\n"
            "- add a task, mark it complete, delete it\n"
            "- filter by all / active / completed\n"
            "- persist to localStorage across reloads\n"
            "- show a count of remaining items\n"
            "- edit an existing task's text\n\n"
            "All HTML, CSS and JavaScript go in the one file. No external dependencies, "
            "no CDN links, no build step. When you are done, say DONE."
        ),
        artifact="todo.html",
        check=check_html_app("todo.html", 2000,
                             ["localstorage", "filter", "complet", "delet"]),
        rubric=(
            "A single-file HTML todo app. Score on: does the required feature set appear to be "
            "fully implemented (add, complete, delete, edit, filter, localStorage persistence, "
            "remaining count); is the JavaScript correct and free of obvious bugs such as stale "
            "closures, broken event delegation or state that desynchronises from storage; is the "
            "markup accessible and the CSS presentable; is the code organised and readable."
        ),
        requirements=[
            "A task can be added",
            "A task can be marked complete",
            "A task can be deleted",
            "An existing task's text can be edited",
            "Tasks can be filtered by all, active and completed",
            "Tasks persist to localStorage and survive a reload",
            "A count of remaining items is displayed",
            "Everything is in the one file, with no external dependency or CDN link",
        ],
        max_turns=30, budget_usd=2.00, timeout_s=420,
    ),

    Task(
        "pacman",
        prompt=(
            "Create pacman.html in the current directory: a single self-contained HTML file "
            "implementing a playable PAC-MAN.\n\n"
            "Requirements:\n"
            "- a maze drawn on a canvas, with walls that block movement\n"
            "- arrow key control with correct wall collision\n"
            "- pellets that score when eaten\n"
            "- at least two ghosts that chase the player\n"
            "- a power pellet that makes ghosts edible for a few seconds\n"
            "- lives, a score display, and a game over state\n\n"
            "All HTML, CSS and JavaScript go in the one file. No external dependencies, "
            "no CDN links, no image files. When you are done, say DONE."
        ),
        artifact="pacman.html",
        check=check_html_app("pacman.html", 6000,
                             ["canvas", "score", "ghost", "requestanimationframe"]),
        rubric=(
            "A single-file HTML PAC-MAN. Score on: is the required feature set actually "
            "implemented (maze with collision, arrow control, scoring pellets, chasing ghosts, "
            "power pellet with an edible window, lives, game over); is the game loop and "
            "collision maths correct; would this plausibly run and be playable rather than "
            "silently erroring; is the code organised and readable."
        ),
        max_turns=50, budget_usd=3.50, timeout_s=600,
    ),

    Task(
        "pelican",
        prompt=(
            "Create pelican.svg in the current directory: a single SVG file showing a pelican "
            "riding a bicycle.\n\n"
            "Hand-author the SVG markup. No external images, no embedded raster data, no fonts. "
            "It should be recognisable as both a pelican and a bicycle. When you are done, say DONE."
        ),
        artifact="pelican.svg",
        check=check_svg,
        rubric=(
            "A hand-authored SVG of a pelican riding a bicycle. Score on: is it recognisably a "
            "pelican (long beak with a pouch, the right body shape) and recognisably a bicycle "
            "(two wheels, frame, handlebars, pedals); is the pelican plausibly positioned as a "
            "rider rather than floating next to the bike; is the drawing detailed and composed "
            "rather than a few crude shapes; is the SVG well-formed and sensibly structured."
        ),
        requirements=[
            "The file is well-formed SVG",
            "A pelican is recognisable, with a long beak and the right body shape",
            "A bicycle is recognisable, with two wheels, a frame and handlebars",
            "The pelican is positioned as riding the bicycle, not beside it",
            "No external images, embedded raster data or fonts are used",
        ],
        max_turns=12, budget_usd=1.50, timeout_s=300,
    ),

    Task(
        "datasci",
        prompt=(
            "Do a small data analysis in the current directory. pandas is installed.\n\n"
            "1. Write generate.py that creates sales.csv with 5000 rows of synthetic retail "
            "sales data: a date spread over two years, a region drawn from 5 values, a product "
            "category drawn from 8 values, units sold, unit price, and a discount percentage. "
            "Build in a seasonal revenue pattern and a handful of anomalous days. Run it.\n"
            "2. Write analyze.py that loads sales.csv with pandas and answers:\n"
            "   - the monthly revenue trend\n"
            "   - the top 3 categories by revenue within each region\n"
            "   - the relationship between discount percentage and units sold\n"
            "   - which dates are anomalous, and on what basis\n"
            "   Run it.\n"
            "3. Write FINDINGS.md reporting what the analysis actually showed, with the numbers.\n\n"
            "When you are done, say DONE."
        ),
        artifact="FINDINGS.md",
        check=check_datasci,
        rubric=(
            "A small synthetic-data analysis. Score on: is the generator realistic and does it "
            "actually build in the seasonality and anomalies it was asked for; is the pandas "
            "analysis correct and does it use appropriate methods rather than hand-rolled loops; "
            "does FINDINGS.md report real numbers produced by the run rather than plausible-looking "
            "invented ones; is the anomaly detection justified on a stated basis; is the writing "
            "specific and quantitative."
        ),
        requirements=[
            "sales.csv is generated with 5000 rows",
            "The data has a date over two years, a region from 5 values, a category "
            "from 8 values, units, unit price and a discount percentage",
            "A seasonal revenue pattern was deliberately built into the generator",
            "Anomalous days were deliberately built into the generator",
            "The monthly revenue trend is reported",
            "The top 3 categories by revenue are reported within each region",
            "The relationship between discount percentage and units sold is reported",
            "Anomalous dates are identified, on a stated basis",
            "FINDINGS.md reports real numbers from the run, not invented ones",
        ],
        judge_files=["FINDINGS.md", "generate.py", "analyze.py"],
        max_turns=45, budget_usd=3.00, timeout_s=600,
    ),

    Task(
        "docs",
        prompt=(
            "jev_ontology.py is in the current directory. It is the decision half of a model "
            "router: it turns a request into a factual ledger, defines the questions a classifier "
            "is asked, and picks a capability tier.\n\n"
            "Read it and write GUIDE.md: a developer guide for somebody who has to modify this "
            "file. Cover what the module is for, the division of labour between code and "
            "classifier, the phases, the question set, the extraction tables, how the tier is "
            "chosen and what the thresholds are, and where somebody should make a change to "
            "alter a routing decision. Be accurate and specific to the code in front of you. "
            "Do not modify jev_ontology.py.\n\n"
            "When you are done, say DONE."
        ),
        artifact="GUIDE.md",
        check=check_prose("GUIDE.md", 2500),
        setup=seed("jev_ontology.py"),
        kind="prose",
        rubric=(
            "A developer guide for a Python module the author was given. Score on: factual "
            "accuracy against the described module (invented function names, wrong thresholds or "
            "hallucinated behaviour are severe); coverage of what a modifier actually needs "
            "(the code/classifier split, phases, question set, extraction tables, tier selection, "
            "where to make a change); whether it cites specific identifiers and numbers rather "
            "than describing the module in vague terms; organisation and scannability; absence "
            "of padding and restated generalities."
        ),
        max_turns=30, budget_usd=2.50, timeout_s=480,
    ),

    Task(
        "secreview",
        prompt=(
            "jev_router.py is in the current directory. It is a FastAPI reverse proxy that sits "
            "in front of an LLM API. It receives client requests carrying the client's own "
            "credential, inspects the request body, calls a third party classifier, rewrites the "
            "model field, and relays the response.\n\n"
            "Do a security review of it and write SECURITY_REVIEW.md.\n\n"
            "For each finding give: the location as filename and line number, a severity, the "
            "concrete attack or failure it enables, and a specific fix. Rank by severity. Prefer "
            "a small number of real findings over a long list of generic advice. If something "
            "looks alarming but is actually safe, say so and say why. Do not modify jev_router.py.\n\n"
            "When you are done, say DONE."
        ),
        artifact="SECURITY_REVIEW.md",
        check=check_prose("SECURITY_REVIEW.md", 2000),
        setup=seed("jev_router.py"),
        kind="prose",
        rubric=(
            "A security review of a reverse proxy the author was given. Score on: are the findings "
            "real and specific to this code rather than generic OWASP boilerplate; are the line "
            "references and described behaviour accurate (a fabricated finding is severe); is the "
            "severity ranking defensible; are the fixes concrete and correct; does it distinguish "
            "genuine issues from things that look alarming but are safe; is it concise rather than "
            "padded with volume."
        ),
        max_turns=35, budget_usd=2.50, timeout_s=540,
    ),

    Task(
        "bugfind",
        prompt=(
            "orders.py in the current directory handles orders and refunds for a "
            "storefront. It is in production.\n\n"
            "Review it for defects and write REVIEW.md.\n\n"
            "For each defect give the function and line number, what goes wrong in "
            "concrete terms, and the fix. Rank by severity. Report only defects you "
            "can point at in the code, not general advice. Do not modify orders.py.\n\n"
            "When you are done, say DONE."
        ),
        artifact="REVIEW.md",
        check=check_prose("REVIEW.md", 500),
        setup=seed_fixture("bugfind", "orders.py"),
        kind="prose",
        rubric=(
            "A defect review of a Python order and refund module. Score on: are the "
            "reported defects real and specific to this code rather than generic advice; "
            "are the line references and described failures accurate; is the severity "
            "ranking defensible; are the fixes correct; is it concise rather than padded."
        ),
        max_turns=30, budget_usd=2.50, timeout_s=480,
    ),

    Task(
        "secfind",
        prompt=(
            "app.py in the current directory is an internal Flask service that serves "
            "customer invoices and admin exports. Any authenticated employee can reach "
            "it.\n\n"
            "Do a security review and write SECURITY.md.\n\n"
            "For each finding give the function and line number, the concrete attack it "
            "enables, a severity, and a specific fix. Rank by severity. Report only "
            "issues you can point at in the code. Do not modify app.py.\n\n"
            "When you are done, say DONE."
        ),
        artifact="SECURITY.md",
        check=check_prose("SECURITY.md", 500),
        setup=seed_fixture("secfind", "app.py"),
        kind="prose",
        rubric=(
            "A security review of a small Flask service. Score on: are the findings real "
            "and specific to this code rather than OWASP boilerplate; are the line "
            "references and described attacks accurate; is the severity ranking "
            "defensible; are the fixes correct and idiomatic; is it concise."
        ),
        max_turns=35, budget_usd=2.50, timeout_s=540,
    ),

    Task(
        "algo",
        prompt=(
            "spec.md in the current directory specifies a function. Implement it in "
            "schedules.py, exactly to the spec.\n\n"
            "Read the rules carefully, including the daylight saving ones. Your code "
            "will be graded by a test suite you cannot see, covering empty input, "
            "unsorted input, touching intervals, zero-length intervals, invalid "
            "intervals, and behaviour across both daylight saving transitions.\n\n"
            "Standard library only. When you are done, say DONE."
        ),
        artifact="schedules.py",
        check=check_prose("schedules.py", 200),
        setup=seed_fixture("algo", "spec.md"),
        rubric=(
            "An implementation of a timezone-aware interval merge, written from a prose "
            "spec. Score on: correctness against the stated rules, especially comparing "
            "real elapsed time rather than wall-clock strings and resolving ambiguous "
            "local times; handling of the edge cases the spec names; clarity; absence of "
            "unnecessary complexity."
        ),
        max_turns=35, budget_usd=2.50, timeout_s=540,
    ),
]

BY_NAME = {t.name: t for t in TASKS}
PILOT = ["todo", "secreview"]

# Tasks with objective ground truth: defects planted on purpose, or a hidden
# test suite. Scoring is a count, not a judgement.
GRADED = ["bugfind", "secfind", "algo"]

# Tasks where a weaker model plausibly fails outright, as opposed to producing
# something rougher. These are the ones that test whether routing costs quality.
DISCRIMINATING = ["bugfind", "secfind", "algo"]
