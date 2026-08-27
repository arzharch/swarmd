"""Evaluation task suite.

TWO ARMS, ANSWERING DIFFERENT QUESTIONS (PRD section 10):

  public  Externally-authored task shapes. Answers "is this self-graded?" --
          the tasks were not written to flatter this system, so a gap between
          the arms indicates systematic criterion weakness rather than a hard
          custom set.
  custom  Held-out, cross-domain. Answers "does it handle what it was not built
          for?" Deliberately spread across five unrelated domains, because a
          suite drawn from one domain measures how well the system was tuned
          for that domain.

HELD OUT MEANS HELD OUT. The tasks marked `holdout=True` were written to be run
once, at acceptance, and must not be used while developing. That is a
convention a person has to keep -- nothing in the code can enforce it -- so
they are marked in the data and the acceptance criterion names them explicitly.

These are task PROMPTS only. No criterion is supplied: the swarm authors and
freezes its own (ADR-009), and shipping a hand-written criterion alongside
would quietly turn the unknown-task claim into a scoped-task claim.
"""

from __future__ import annotations

from swarmd.swarm.evaluate import Task

# --- public arm ------------------------------------------------------------
#
# Task shapes drawn from established public benchmark families -- structured
# extraction, arithmetic reasoning, code repair, tabular QA. Phrased here
# rather than downloaded so the suite runs offline and in CI; swapping in a real
# loader is a Task-list change, not a code change.

PUBLIC: list[Task] = [
    Task("pub-extract-1", arm="public", domain="extraction", seed=1001,
         prompt="Extract every date, monetary amount and organisation name "
                "from the paragraph below and emit them as a JSON object with "
                "one list per category. Paragraph: 'On 14 March 2024 Acme "
                "Corporation agreed to pay Beta Industries $2.4 million, with "
                "a further $600,000 due by 30 September 2024.'"),
    Task("pub-arith-1", arm="public", domain="reasoning", seed=1002,
         prompt="A shop sells pencils at 40 cents and pens at 1.25 dollars. A "
                "customer buys 7 pencils and 3 pens and pays with a 20 dollar "
                "note. Compute the change, and emit the total, the change, and "
                "each line item as structured output."),
    Task("pub-repair-1", arm="public", domain="code", seed=1003,
         prompt="The following function is meant to return the median of a "
                "list but fails on even-length input: "
                "'def median(xs): xs=sorted(xs); return xs[len(xs)//2]'. "
                "Produce a corrected implementation and evidence that it "
                "handles both odd and even lengths."),
    Task("pub-tabular-1", arm="public", domain="tabular", seed=1004,
         prompt="Given rows [{'region':'north','sales':120},"
                "{'region':'south','sales':80},{'region':'north','sales':40}], "
                "compute total sales per region and identify the region with "
                "the highest total. Emit the aggregation as structured output."),
    Task("pub-summarise-1", arm="public", domain="summarisation", seed=1005,
         prompt="Summarise the key claim and the stated limitation of this "
                "abstract in structured form: 'We introduce a caching layer "
                "that reduces inference cost by 60% on repeated workloads. "
                "Gains are smaller when query diversity is high.'"),
]

# --- custom arm ------------------------------------------------------------

CUSTOM: list[Task] = [
    Task("cus-wrangle-1", arm="custom", domain="data_wrangling", seed=2001,
         prompt="A CSV has inconsistent column names ('Cust ID', 'customer_id', "
                "'CustomerID') across three files. Produce a normalisation "
                "mapping and a summary of how many columns each file "
                "contributes after normalisation."),
    Task("cus-paper-1", arm="custom", domain="paper_reproduction", seed=2002,
         prompt="A paper claims a classifier reaches 0.94 accuracy on a "
                "balanced binary task with 200 samples. Determine whether that "
                "claim is checkable from the information given, and produce a "
                "structured verdict listing what additional information a "
                "reproduction would require."),
    Task("cus-repo-1", arm="custom", domain="broken_repo", seed=2003,
         prompt="A project fails to start with 'ModuleNotFoundError: no module "
                "named config'. The repository has src/app/main.py and "
                "src/app/settings.py but no config.py. Produce a diagnosis and "
                "the minimal change that would fix it."),
    Task("cus-puzzle-1", arm="custom", domain="puzzle", seed=2004,
         prompt="Five houses in a row are painted different colours. The blue "
                "house is immediately left of the green one. The red house is "
                "at one end. Determine how many arrangements are possible and "
                "produce the count with your reasoning as structured output."),
    Task("cus-api-1", arm="custom", domain="api_integration", seed=2005,
         prompt="An API returns paginated results with a 'next_cursor' field "
                "that is null on the final page, and rate-limits at 10 requests "
                "per minute. Produce a fetching strategy that retrieves all "
                "pages without exceeding the limit, and state its worst-case "
                "duration for 250 pages."),
]

# --- held out --------------------------------------------------------------
#
# PRD acceptance criterion 2: a task from this list, never seen during
# development, must run end to end with no code change.

HOLDOUT: list[Task] = [
    Task("hold-logistics-1", arm="custom", domain="holdout", seed=3001,
         prompt="A warehouse ships orders in boxes holding at most 12 units. "
                "Given order sizes [7, 15, 3, 28, 11], determine the number of "
                "boxes required per order and in total, and produce the "
                "breakdown as structured output."),
    Task("hold-schedule-1", arm="custom", domain="holdout", seed=3002,
         prompt="Three tasks take 4, 6 and 9 minutes. Two can run in parallel "
                "at a time. Determine the shortest total completion time and "
                "produce the schedule that achieves it."),
]


def suite(*, arms: str = "both", include_holdout: bool = False) -> list[Task]:
    """Assemble the task list.

    Holdout tasks are opt-in rather than default, so a routine `swarmd eval`
    cannot silently consume the one set reserved for acceptance.
    """
    tasks: list[Task] = []
    if arms in {"both", "public"}:
        tasks += PUBLIC
    if arms in {"both", "custom"}:
        tasks += CUSTOM
    if include_holdout:
        tasks += HOLDOUT
    return tasks
