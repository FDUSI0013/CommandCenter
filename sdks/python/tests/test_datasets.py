"""Datasets and experiments — the offline half of the loop."""

from __future__ import annotations

from fulcrum_ops import DatasetItem, FulcrumOps

from .conftest import sent
from .stub_server import StubServer


def test_a_dataset_is_created_and_filled(client: FulcrumOps, stub: StubServer) -> None:
    client.datasets.create("checkout-questions", description="Golden set", tags=["q3"])
    added = client.datasets.add_items(
        "checkout-questions",
        [
            {"input": "where is my order?", "expected_output": "tracking link"},
            ("can I return this?", "returns policy"),
            DatasetItem(input="do you ship to NL?", expected_output="yes", metadata={"tier": "a"}),
            "a bare string is a case too",
        ],
    )

    assert added == 4
    cases = client.datasets.items("checkout-questions")
    assert [case.input for case in cases] == [
        "where is my order?",
        "can I return this?",
        "do you ship to NL?",
        "a bare string is a case too",
    ]
    assert cases[2].metadata == {"tier": "a"}
    assert cases[3].expected_output is None


def test_items_are_chunked_to_what_the_endpoint_accepts(
    client: FulcrumOps, stub: StubServer
) -> None:
    client.datasets.create("big")
    client.datasets.add_items("big", [{"input": str(n)} for n in range(250)], chunk_size=100)

    posts = [
        r
        for r in stub.requests
        if r.method == "POST" and r.path == "/api/v1/evaluations/datasets/big/items"
    ]
    assert [len(r.body["items"]) for r in posts] == [100, 100, 50]


def test_evaluate_traces_every_case_and_scores_it(
    client: FulcrumOps, stub: StubServer
) -> None:
    client.datasets.create("qa")
    client.datasets.add_items(
        "qa",
        [
            {"input": "where is my order?", "expected_output": "tracking"},
            {"input": "can I return this?", "expected_output": "returns"},
        ],
    )

    def task(question: str) -> str:
        return "here is your tracking link" if "order" in question else "no idea"

    def contains_expected(output, expected):
        return 1.0 if expected and expected in str(output) else 0.0

    result = client.evaluate("qa", task, scorers=[contains_expected], name="run-1")

    assert result.total == 2
    assert result.failures == 0
    assert result.averages == {"contains_expected": 0.5}
    assert result.summary()["cases"] == 2

    traces = sent(stub, "traces")
    assert len(traces) == 2
    assert all(row["name"] == "run-1" for row in traces)
    assert all("experiment" in row["tags"] for row in traces)
    assert traces[0]["metadata"]["dataset"] == "qa"
    assert traces[0]["feedback_scores"][0]["name"] == "contains_expected"
    assert traces[0]["feedback_scores"][0]["source"] == "experiment"


def test_a_failing_case_is_recorded_and_the_run_continues(
    client: FulcrumOps, stub: StubServer
) -> None:
    """Finishing and showing all four failures beats stopping at the first."""

    def task(question: str) -> str:
        if question == "boom":
            raise RuntimeError("the agent fell over")
        return "fine"

    result = client.evaluate(
        "inline",
        task,
        items=["ok", "boom", "also ok"],
        scorers=[lambda output, expected: 1.0],
    )

    assert result.total == 3
    assert result.failures == 1
    assert "RuntimeError: the agent fell over" in result.rows[1].error
    assert result.averages == {"<lambda>": 1.0}

    traces = {row["name"]: row for row in sent(stub, "traces")}
    failed = [row for row in sent(stub, "traces") if "error_info" in row]
    assert len(failed) == 1
    assert failed[0]["error_info"]["exception_type"] == "RuntimeError"


def test_a_broken_scorer_does_not_end_the_run(client: FulcrumOps) -> None:
    def explodes(output, expected):
        raise ValueError("bad scorer")

    def works(output, expected):
        return 0.5

    result = client.evaluate("inline", lambda text: text, items=["a"], scorers=[explodes, works])

    assert result.failures == 0
    assert result.averages == {"works": 0.5}


def test_a_task_is_called_the_way_its_signature_asks(client: FulcrumOps) -> None:
    seen: list = []

    def one_arg(text):
        seen.append(("one", text))
        return text

    def two_args(text, expected):
        seen.append(("two", text, expected))
        return text

    def by_case(item):
        seen.append(("case", item.input, item.metadata))
        return item.input

    items = [DatasetItem(input="q", expected_output="e", metadata={"k": "v"})]
    for task in (one_arg, two_args, by_case):
        client.evaluate("inline", task, items=items)

    assert seen == [("one", "q"), ("two", "q", "e"), ("case", "q", {"k": "v"})]


def test_scorer_return_shapes_are_all_understood(client: FulcrumOps) -> None:
    result = client.evaluate(
        "inline",
        lambda text: text,
        items=["a"],
        scorers=[
            lambda o, e: True,
            lambda o, e: 0.25,
            lambda o, e: (0.75, "because"),
            lambda o, e: {"name": "named", "value": 1.0, "reason": "why"},
            lambda o, e: {"first": 0.1, "second": 0.2},
            lambda o, e: None,
        ],
    )

    scores = result.rows[0].scores
    assert scores["<lambda>"] == 0.75, "later lambdas share a name; the last one wins"
    assert scores["named"] == 1.0
    assert scores["first"] == 0.1 and scores["second"] == 0.2
    assert result.rows[0].reasons["<lambda>"] == "because"


def test_a_server_side_evaluation_is_started_and_read(client: FulcrumOps) -> None:
    started = client.experiments.run("qa", judge_model="gpt-4o", name="nightly")
    assert started["id"] == "eval-1"
    assert started["dataset"] == "qa"
    assert started["judge_model"] == "gpt-4o"

    fetched = client.experiments.get("eval-1")
    assert fetched["status"] == "Completed"
