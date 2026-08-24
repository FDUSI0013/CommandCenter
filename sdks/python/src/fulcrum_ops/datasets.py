"""Datasets and experiments — the offline half of the loop.

Tracing tells you what your agent did in production. This module is for the
question you ask before shipping: *did the change make it better?* A dataset is
a fixed set of cases; an experiment runs the agent over all of them and scores
the results; the scores land on real traces, so a regression shows up in the
same console screens as everything else rather than in a notebook nobody else
can see.

Two ways to run one, and they are different tools:

* :meth:`Experiments.run` asks the control plane to evaluate a dataset with a
  judge model. The work happens server-side; you get an evaluation id back.
* :meth:`Experiments.evaluate` runs *your* function over the cases in *your*
  process, traces every case, and applies scorers you wrote in Python. Use it
  when the thing being evaluated is code that only exists on your machine.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .transport import Transport

__all__ = ["Datasets", "Experiments", "ExperimentResult", "ExperimentRow", "DatasetItem"]

logger = logging.getLogger("fulcrum_ops")

#: What a scorer is handed and what it may return. A float is the score; a dict
#: is several named scores; a ``(value, reason)`` pair carries the explanation
#: that makes a low score actionable.
ScorerResult = Any
Scorer = Callable[..., ScorerResult]


@dataclass
class DatasetItem:
    """One case: an input, optionally what a correct answer looks like."""

    input: str
    expected_output: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"input": self.input}
        if self.expected_output is not None:
            body["expected_output"] = self.expected_output
        if self.metadata:
            body["metadata"] = self.metadata
        return body

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "DatasetItem":
        return cls(
            input=str(record.get("input") or ""),
            expected_output=record.get("expected_output"),
            metadata=dict(record.get("metadata") or {}),
            id=record.get("id"),
        )


@dataclass
class ExperimentRow:
    """What happened to one case."""

    item: DatasetItem
    output: Any = None
    trace_id: Optional[str] = None
    scores: Dict[str, float] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ExperimentResult:
    """The aggregate, plus every row that produced it."""

    name: str
    dataset: str
    rows: List[ExperimentRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def failures(self) -> int:
        return sum(1 for row in self.rows if not row.ok)

    @property
    def averages(self) -> Dict[str, float]:
        """Mean of each score across the rows that produced it."""
        totals: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        for row in self.rows:
            for name, value in row.scores.items():
                totals[name] = totals.get(name, 0.0) + value
                counts[name] = counts.get(name, 0) + 1
        return {name: totals[name] / counts[name] for name in totals if counts[name]}

    def summary(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dataset": self.dataset,
            "cases": self.total,
            "failures": self.failures,
            "scores": self.averages,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "ExperimentResult(name={0!r}, cases={1}, failures={2}, scores={3})".format(
            self.name, self.total, self.failures, self.averages
        )


class Datasets:
    """``client.datasets`` — the evaluation datasets in this workspace."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list(self, *, query: Optional[str] = None, page_size: int = 50) -> List[Dict[str, Any]]:
        """Every dataset, following pagination."""
        return list(self._paginate("evaluations/datasets", {"q": query}, page_size))

    def create(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Create a dataset. Raises if the name is already taken."""
        body: Dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if tags:
            body["tags"] = list(tags)
        result = self._transport.request_json("POST", "evaluations/datasets", body=body)
        return result if isinstance(result, dict) else {"name": name}

    def add_items(
        self,
        dataset: str,
        items: Iterable[Any],
        *,
        chunk_size: int = 500,
    ) -> int:
        """Append cases to a dataset, in chunks the endpoint will accept.

        Accepts :class:`DatasetItem`, plain dicts, or ``(input, expected)``
        pairs — whatever the caller already has in hand.
        """
        payloads = [_coerce_item(item).to_payload() for item in items]
        sent = 0
        for start in range(0, len(payloads), max(1, chunk_size)):
            chunk = payloads[start : start + max(1, chunk_size)]
            if not chunk:
                continue
            self._transport.request_json(
                "POST",
                "evaluations/datasets/{0}/items".format(dataset),
                body={"items": chunk},
            )
            sent += len(chunk)
        return sent

    def items(self, dataset: str, *, page_size: int = 100) -> List[DatasetItem]:
        """Every case in a dataset, following pagination."""
        records = self._paginate(
            "evaluations/datasets/{0}/items".format(dataset), None, min(page_size, 100)
        )
        return [DatasetItem.from_record(record) for record in records]

    def _paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]],
        page_size: int,
    ) -> Iterator[Dict[str, Any]]:
        page = 1
        while True:
            query = {"page": page, "page_size": page_size}
            for key, value in (params or {}).items():
                if value is not None:
                    query[key] = value
            body = self._transport.request_json("GET", path, params=query)
            if not isinstance(body, dict):
                return
            items = body.get("items") or []
            for item in items:
                if isinstance(item, dict):
                    yield item
            pages = int(body.get("pages") or 1)
            if page >= pages or not items:
                return
            page += 1


class Experiments:
    """``client.experiments`` — evaluation runs, server-side or local."""

    def __init__(self, transport: Transport, client: Any) -> None:
        self._transport = transport
        self._client = client

    # ---------------------------------------------------------- server-side

    def run(
        self,
        dataset: str,
        *,
        agent_id: Optional[str] = None,
        judge_model: str = "gpt-4o",
        name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ask the control plane to evaluate a dataset with a judge model."""
        body: Dict[str, Any] = {"dataset": dataset, "judge_model": judge_model}
        if agent_id:
            body["agent_id"] = agent_id
        if name:
            body["name"] = name
        if notes:
            body["notes"] = notes
        result = self._transport.request_json("POST", "evaluations", body=body)
        return result if isinstance(result, dict) else {}

    def get(self, evaluation_id: str) -> Dict[str, Any]:
        """Fetch one evaluation, including its scores once it has finished."""
        result = self._transport.request_json("GET", "evaluations/{0}".format(evaluation_id))
        return result if isinstance(result, dict) else {}

    def list(
        self,
        *,
        dataset: Optional[str] = None,
        status: Optional[str] = None,
        page_size: int = 25,
    ) -> List[Dict[str, Any]]:
        """Recent evaluations, newest first."""
        params: Dict[str, Any] = {"page_size": page_size, "sort": "-occurred_at"}
        if dataset:
            params["dataset"] = dataset
        if status:
            params["status"] = status
        body = self._transport.request_json("GET", "evaluations", params=params)
        items = body.get("items") if isinstance(body, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    # ---------------------------------------------------------------- local

    def evaluate(
        self,
        dataset: str,
        task: Callable[..., Any],
        *,
        scorers: Optional[Sequence[Scorer]] = None,
        name: Optional[str] = None,
        items: Optional[Sequence[Any]] = None,
        agent: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        flush: bool = True,
    ) -> ExperimentResult:
        """Run ``task`` over every case in a dataset, tracing and scoring each one.

        Every case becomes a real trace tagged with the experiment name, and
        every scorer's verdict becomes a feedback score on that trace — so the
        run is visible in the Evaluations and Runs screens next to production
        traffic, not just in this function's return value.

        A case that raises is recorded as a failed trace and the run continues.
        An experiment that stops at the first bad case tells you far less than
        one that finishes and shows you all four.
        """
        cases = (
            [_coerce_item(item) for item in items]
            if items is not None
            else Datasets(self._transport).items(dataset)
        )
        experiment_name = name or "experiment:{0}".format(dataset)
        result = ExperimentResult(name=experiment_name, dataset=dataset)
        scorer_list = list(scorers or [])
        run_tags = list(tags or []) + ["experiment", experiment_name]

        for case in cases:
            row = ExperimentRow(item=case)
            with self._client.trace(
                experiment_name,
                input={"input": case.input, "expected_output": case.expected_output},
                agent=agent,
                tags=run_tags,
                metadata={"dataset": dataset, "dataset_item_id": case.id},
            ) as trace:
                row.trace_id = trace.id
                try:
                    row.output = _call_task(task, case)
                    trace.set_output(row.output)
                except Exception as exc:  # noqa: BLE001 - one bad case must not end the run
                    row.error = "{0}: {1}".format(type(exc).__name__, exc)
                    trace.record_exception(exc)
                    logger.warning("fulcrum-ops: experiment case failed — %s", row.error)

                if row.ok:
                    for scorer in scorer_list:
                        for score_name, value, reason in _apply_scorer(scorer, case, row.output):
                            row.scores[score_name] = value
                            if reason:
                                row.reasons[score_name] = reason
                            # "sdk" and not "experiment": the telemetry store's
                            # source enum refuses anything else, batch-wide.
                            trace.score(score_name, value, reason=reason, source="sdk")

            result.rows.append(row)

        if flush:
            self._client.flush()
        return result


# --------------------------------------------------------------------- helpers


def _coerce_item(item: Any) -> DatasetItem:
    if isinstance(item, DatasetItem):
        return item
    if isinstance(item, dict):
        return DatasetItem.from_record(item)
    if isinstance(item, (tuple, list)) and item:
        expected = item[1] if len(item) > 1 else None
        return DatasetItem(input=str(item[0]), expected_output=expected)
    return DatasetItem(input=str(item))


def _call_task(task: Callable[..., Any], case: DatasetItem) -> Any:
    """Call the task the way its signature says it wants to be called.

    Most people write ``def task(input)``; some write ``def task(item)`` because
    they need the metadata, and some write ``def task(input, expected)``.
    Reading the signature rather than catching ``TypeError`` matters: a
    ``TypeError`` raised *inside* the task is a real failure of that case, and
    retrying it with different arguments would hide the bug.
    """
    try:
        parameters = inspect.signature(task).parameters
    except (TypeError, ValueError):  # builtins and C callables have no signature
        return task(case.input)

    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters.values()):
        return task(case.input, case.expected_output)
    if len(positional) >= 2:
        return task(case.input, case.expected_output)
    if len(positional) == 1 and positional[0].name in ("item", "case", "row"):
        return task(case)
    return task(case.input)


def _apply_scorer(
    scorer: Scorer, case: DatasetItem, output: Any
) -> List[Tuple[str, float, Optional[str]]]:
    """Run one scorer and normalise whatever it returned into named scores."""
    label = getattr(scorer, "name", None) or getattr(scorer, "__name__", None) or "score"
    arguments: List[Any] = [output, case.expected_output, case]
    try:
        parameters = inspect.signature(scorer).parameters
        if not any(
            p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters.values()
        ):
            wanted = sum(
                1
                for p in parameters.values()
                if p.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            )
            arguments = arguments[: max(1, min(3, wanted))]
    except (TypeError, ValueError):
        arguments = [output, case.expected_output]

    try:
        result = scorer(*arguments)
    except Exception as exc:  # noqa: BLE001 - a broken scorer must not end the run
        logger.warning("fulcrum-ops: scorer %r raised — %s", label, exc)
        return []

    return _normalise_score(label, result)


def _normalise_score(label: str, result: Any) -> List[Tuple[str, float, Optional[str]]]:
    if result is None:
        return []
    if isinstance(result, bool):
        return [(label, 1.0 if result else 0.0, None)]
    if isinstance(result, (int, float)):
        return [(label, float(result), None)]
    if isinstance(result, tuple) and len(result) == 2:
        value, reason = result
        try:
            return [(label, float(value), str(reason) if reason is not None else None)]
        except (TypeError, ValueError):
            return []
    if isinstance(result, dict):
        # Either {"name": ..., "value": ...} or {"score_a": 1.0, "score_b": 0.5}.
        if "value" in result:
            try:
                return [
                    (
                        str(result.get("name") or label),
                        float(result["value"]),
                        str(result["reason"]) if result.get("reason") else None,
                    )
                ]
            except (TypeError, ValueError):
                return []
        out: List[Tuple[str, float, Optional[str]]] = []
        for key, value in result.items():
            try:
                out.append((str(key), float(value), None))
            except (TypeError, ValueError):
                continue
        return out
    return []
